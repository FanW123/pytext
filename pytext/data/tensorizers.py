#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

from typing import List, Optional

import torch
from pytext.common import Padding
from pytext.config.component import Component, ComponentType, create_component
from pytext.data.data_structures.annotation import (
    REDUCE,
    SHIFT,
    Annotation,
    is_intent_nonterminal,
    is_slot_nonterminal,
    is_unsupported,
    is_valid_nonterminal,
)
from pytext.data.sources.data_source import Gazetteer
from pytext.data.tokenizers import Token, Tokenizer
from pytext.utils import cuda, torch as utils_torch
from pytext.utils.data import Slot

from .utils import (
    BOL,
    EOL,
    PAD,
    SpecialToken,
    VocabBuilder,
    Vocabulary,
    align_target_label,
    pad_and_tensorize,
)


def tokenize(
    text: str = None,
    pre_tokenized: List[Token] = None,
    tokenizer: Tokenizer = None,
    bos_token: Optional[str] = None,
    eos_token: Optional[str] = None,
    pad_token: str = PAD,
    use_eos_token_for_bos: bool = False,
    max_seq_len: int = 2 ** 30,
):
    tokenized = (
        pre_tokenized
        or tokenizer.tokenize(text)[
            : max_seq_len - (bos_token is not None) - (eos_token is not None)
        ]
    )
    if bos_token:
        if use_eos_token_for_bos:
            bos_token = eos_token
        tokenized = [Token(bos_token, -1, -1)] + tokenized
    if eos_token:
        tokenized.append(Token(eos_token, -1, -1))
    if not tokenized:
        tokenized = [Token(pad_token, -1, -1)]

    tokenized_texts, start_idx, end_idx = zip(
        *((t.value, t.start, t.end) for t in tokenized)
    )
    return tokenized_texts, start_idx, end_idx


def lookup_tokens(
    text: str = None,
    pre_tokenized: List[Token] = None,
    tokenizer: Tokenizer = None,
    vocab: Vocabulary = None,
    bos_token: Optional[str] = None,
    eos_token: Optional[str] = None,
    pad_token: str = PAD,
    use_eos_token_for_bos: bool = False,
    max_seq_len: int = 2 ** 30,
):
    tokenized_texts, start_idx, end_idx = tokenize(
        text,
        pre_tokenized,
        tokenizer,
        bos_token,
        eos_token,
        pad_token,
        use_eos_token_for_bos,
        max_seq_len,
    )
    tokens = vocab.lookup_all(tokenized_texts)
    return tokens, start_idx, end_idx


class Tensorizer(Component):
    """Tensorizers are a component that converts from batches of
    `pytext.data.type.DataType` instances to tensors. These tensors will eventually
    be inputs to the model, but the model is aware of the tensorizers and can arrange
    the tensors they create to conform to its model.

    Tensorizers have an initialize function. This function allows the tensorizer to
    read through the training dataset to build up any data that it needs for
    creating the model. Commonly this is valuable for things like inferring a
    vocabulary from the training set, or learning the entire set of training labels,
    or slot labels, etc.
    """

    __COMPONENT_TYPE__ = ComponentType.TENSORIZER
    __EXPANSIBLE__ = True

    class Config(Component.Config):
        pass

    # Indicate if it can be used to generate input Tensors for prediction
    is_input = True

    @property
    def column_schema(self):
        """Generic types don't pickle well pre-3.7, so we don't actually want
        to store the schema as an attribute. We're already storing all of the
        columns anyway, so until there's a better solution, schema is a property."""
        return []

    def numberize(self, row):
        raise NotImplementedError

    def prepare_input(self, row):
        """ Return preprocessed input tensors/blob for caffe2 prediction net."""
        self.numberize(row)

    def sort_key(self, row):
        raise NotImplementedError

    def tensorize(self, batch):
        """Tensorizer knows how to pad and tensorize a batch of it's own output."""
        return batch

    def initialize(self):
        """
        The initialize function is carefully designed to allow us to read through the
        training dataset only once, and not store it in memory. As such, it can't itself
        manually iterate over the data source. Instead, the initialize function is a
        coroutine, which is sent row data. This should look roughly like::

            # set up variables here
            ...
            try:
                # start reading through data source
                while True:
                    # row has type Dict[str, types.DataType]
                    row = yield
                    # update any variables, vocabularies, etc.
                    ...
            except GeneratorExit:
                # finalize your initialization, set instance variables, etc.
                ...

        See `WordTokenizer.initialize` for a more concrete example.
        """
        return
        # we need yield here to make this function a generator
        yield


class VocabFileConfig(Component.Config):
    #: File containing tokens to add to vocab (first whitespace-separated entry per
    #: line)
    filepath: str = ""
    #: Whether to skip the first line of the file (e.g. if it is a header line)
    skip_header_line: bool = False
    #: Whether to lowercase each of the tokens in the file
    lowercase_tokens: bool = False
    #: The max number of tokens to add to vocab
    size_limit: int = 0


class VocabConfig(Component.Config):
    #: Whether to add tokens from training data to vocab.
    build_from_data: bool = True
    #: Add `size_from_data` most frequent tokens in training data to vocab (if this
    #: is 0, add all tokens from training data).
    size_from_data: int = 0
    vocab_files: List[VocabFileConfig] = []


class TokenTensorizer(Tensorizer):
    """Convert text to a list of tokens. Do this based on a tokenizer configuration,
    and build a vocabulary for numberization. Finally, pad the batch to create
    a square tensor of the correct size.
    """

    class Config(Tensorizer.Config):
        #: The name of the text column to parse from the data source.
        column: str = "text"
        #: The tokenizer to use to split input text into tokens.
        tokenizer: Tokenizer.Config = Tokenizer.Config()
        add_bos_token: bool = False
        add_eos_token: bool = False
        use_eos_token_for_bos: bool = False
        max_seq_len: Optional[int] = None
        vocab: VocabConfig = VocabConfig()

    @classmethod
    def from_config(cls, config: Config):
        tokenizer = create_component(ComponentType.TOKENIZER, config.tokenizer)
        return cls(
            text_column=config.column,
            tokenizer=tokenizer,
            add_bos_token=config.add_bos_token,
            add_eos_token=config.add_eos_token,
            use_eos_token_for_bos=config.use_eos_token_for_bos,
            max_seq_len=config.max_seq_len,
            vocab_config=config.vocab,
        )

    def __init__(
        self,
        text_column,
        tokenizer=None,
        add_bos_token=Config.add_bos_token,
        add_eos_token=Config.add_eos_token,
        use_eos_token_for_bos=Config.use_eos_token_for_bos,
        max_seq_len=Config.max_seq_len,
        vocab_config=None,
        vocab=None,
    ):
        self.text_column = text_column
        self.tokenizer = tokenizer or Tokenizer()
        self.vocab = vocab
        self.add_bos_token = add_bos_token
        self.add_eos_token = add_eos_token
        self.use_eos_token_for_bos = use_eos_token_for_bos
        self.max_seq_len = max_seq_len or 2 ** 30  # large number
        self.vocab_builder = None
        self.vocab_config = vocab_config or VocabConfig()

    @property
    def column_schema(self):
        return [(self.text_column, str)]

    def _tokenize(self, text=None, pre_tokenized=None):
        return tokenize(
            text=text,
            pre_tokenized=pre_tokenized,
            tokenizer=self.tokenizer,
            bos_token=self.vocab.bos_token if self.add_bos_token else None,
            eos_token=self.vocab.eos_token if self.add_eos_token else None,
            pad_token=self.vocab.pad_token,
            use_eos_token_for_bos=self.use_eos_token_for_bos,
            max_seq_len=self.max_seq_len,
        )

    def _lookup_tokens(self, text=None, pre_tokenized=None):
        return lookup_tokens(
            text=text,
            pre_tokenized=pre_tokenized,
            tokenizer=self.tokenizer,
            vocab=self.vocab,
            bos_token=self.vocab.bos_token if self.add_bos_token else None,
            eos_token=self.vocab.eos_token if self.add_eos_token else None,
            pad_token=self.vocab.pad_token,
            use_eos_token_for_bos=self.use_eos_token_for_bos,
            max_seq_len=self.max_seq_len,
        )

    def _reverse_lookup(self, token_ids):
        return [self.vocab[id] for id in token_ids]

    def initialize(self, vocab_builder=None):
        """Build vocabulary based on training corpus."""
        if self.vocab:
            if self.vocab_config.build_from_data or self.vocab_config.vocab_files:
                print(
                    f"`{self.text_column}` column: vocab already provided, skipping "
                    f"adding tokens from data and from vocab files."
                )
            return

        if not self.vocab_config.build_from_data and not self.vocab_config.vocab_files:
            raise ValueError(
                f"To create token tensorizer for '{self.text_column}', either "
                f"`build_from_data` or `vocab_files` must be set."
            )

        self.vocab_builder = vocab_builder or VocabBuilder()
        self.vocab_builder.use_bos = self.add_bos_token
        self.vocab_builder.use_eos = self.add_eos_token
        if not self.vocab_config.build_from_data:
            self._add_vocab_from_files()
            self.vocab = self.vocab_builder.make_vocab()
            return

        try:
            while True:
                row = yield
                raw_text = row[self.text_column]
                tokenized = self.tokenizer.tokenize(raw_text)
                self.vocab_builder.add_all([t.value for t in tokenized])
        except GeneratorExit:
            self.vocab_builder.truncate_to_vocab_size(self.vocab_config.size_from_data)
            self._add_vocab_from_files()
            self.vocab = self.vocab_builder.make_vocab()

    def _add_vocab_from_files(self):
        for vocab_file in self.vocab_config.vocab_files:
            with open(vocab_file.filepath) as f:
                self.vocab_builder.add_from_file(
                    f,
                    vocab_file.skip_header_line,
                    vocab_file.lowercase_tokens,
                    vocab_file.size_limit,
                )

    def numberize(self, row):
        """Tokenize, look up in vocabulary."""
        tokens, start_idx, end_idx = self._lookup_tokens(row[self.text_column])
        token_ranges = list(zip(start_idx, end_idx))
        return tokens, len(tokens), token_ranges

    def prepare_input(self, row):
        """Tokenize, look up in vocabulary, return tokenized_texts in raw text"""
        tokenized_texts, start_idx, end_idx = self._tokenize(row[self.text_column])
        token_ranges = list(zip(start_idx, end_idx))
        return tokenized_texts, len(tokenized_texts), token_ranges

    def tensorize(self, batch):
        tokens, seq_lens, token_ranges = zip(*batch)
        return (
            pad_and_tensorize(tokens, self.vocab.get_pad_index()),
            pad_and_tensorize(seq_lens),
            pad_and_tensorize(token_ranges),
        )

    def sort_key(self, row):
        # use seq_len as sort key
        return row[1]


class ByteTensorizer(Tensorizer):
    """Turn characters into sequence of int8 bytes. One character will have one
    or more bytes depending on it's encoding
    """

    UNK_BYTE = 0
    PAD_BYTE = 0
    NUM = 256

    class Config(Tensorizer.Config):
        #: The name of the text column to parse from the data source.
        column: str = "text"
        lower: bool = True
        max_seq_len: Optional[int] = None

    @classmethod
    def from_config(cls, config: Config):
        return cls(config.column, config.lower, config.max_seq_len)

    def __init__(self, text_column, lower=True, max_seq_len=None):
        self.text_column = text_column
        self.lower = lower
        self.max_seq_len = max_seq_len

    @property
    def column_schema(self):
        return [(self.text_column, str)]

    def numberize(self, row):
        """Convert text to characters."""
        text = row[self.text_column]
        if self.lower:
            text = text.lower()
        bytes = list(text.encode())
        if self.max_seq_len:
            bytes = bytes[: self.max_seq_len]
        return bytes, len(bytes)

    def tensorize(self, batch):
        bytes, bytes_len = zip(*batch)
        return pad_and_tensorize(bytes, self.PAD_BYTE), pad_and_tensorize(bytes_len)

    def sort_key(self, row):
        # use bytes_len as sort key
        return row[1]


class ByteTokenTensorizer(Tensorizer):
    """Turn words into 2-dimensional tensors of int8 bytes. Words are padded to
    `max_byte_len`. Also computes sequence lengths (1-D tensor) and token lengths
    (2-D tensor). 0 is the pad byte.
    """

    NUM_BYTES = 256

    class Config(Tensorizer.Config):
        #: The name of the text column to parse from the data source.
        column: str = "text"
        #: The tokenizer to use to split input text into tokens.
        tokenizer: Tokenizer.Config = Tokenizer.Config()
        #: The max token length for input text.
        max_seq_len: Optional[int] = None
        #: The max byte length for a token.
        max_byte_len: int = 15
        #: Offset to add to all non-padding bytes
        offset_for_non_padding: int = 0

    @classmethod
    def from_config(cls, config: Config):
        tokenizer = create_component(ComponentType.TOKENIZER, config.tokenizer)
        return cls(
            text_column=config.column,
            tokenizer=tokenizer,
            max_seq_len=config.max_seq_len,
            max_byte_len=config.max_byte_len,
            offset_for_non_padding=config.offset_for_non_padding,
        )

    def __init__(
        self,
        text_column,
        tokenizer=None,
        max_seq_len=Config.max_seq_len,
        max_byte_len=Config.max_byte_len,
        offset_for_non_padding=Config.offset_for_non_padding,
    ):
        self.text_column = text_column
        self.tokenizer = tokenizer or Tokenizer()
        self.max_seq_len = max_seq_len or 2 ** 30  # large number
        self.max_byte_len = max_byte_len
        self.offset_for_non_padding = offset_for_non_padding

    @property
    def column_schema(self):
        return [(self.text_column, str)]

    def numberize(self, row):
        """Convert text to bytes, pad batch."""
        tokens = self.tokenizer.tokenize(row[self.text_column])[: self.max_seq_len]
        if not tokens:
            tokens = [Token(PAD, -1, -1)]
        bytes = [self._numberize_token(token)[: self.max_byte_len] for token in tokens]
        token_lengths = len(tokens)
        byte_lengths = [len(token_bytes) for token_bytes in bytes]
        return bytes, token_lengths, byte_lengths

    def _numberize_token(self, token):
        return [c + self.offset_for_non_padding for c in token.value.encode()]

    def tensorize(self, batch):
        bytes, token_lengths, byte_lengths = zip(*batch)
        pad_shape = (len(batch), max(len(l) for l in byte_lengths), self.max_byte_len)
        return (
            pad_and_tensorize(bytes, pad_shape=pad_shape),
            pad_and_tensorize(token_lengths),
            pad_and_tensorize(byte_lengths),
        )

    def sort_key(self, row):
        return len(row[0])


class CharacterTokenTensorizer(TokenTensorizer):
    """Turn words into 2-dimensional tensors of ints based on their ascii values.
    Words are padded to the maximum word length (also capped at `max_char_length`).
    Sequence lengths are the length of each token, 0 for pad token.
    """

    class Config(TokenTensorizer.Config):
        #: The max character length for a token.
        max_char_length: int = 20

    def __init__(self, max_char_length: int = Config.max_char_length, **kwargs):
        self.max_char_length = max_char_length
        super().__init__(**kwargs)

    # Don't need to create a vocab
    initialize = Tensorizer.initialize

    def numberize(self, row):
        """Convert text to characters, pad batch."""
        tokens = self.tokenizer.tokenize(row[self.text_column])[: self.max_seq_len]
        characters = [
            self._numberize_token(token)[: self.max_char_length] for token in tokens
        ]
        token_lengths = len(tokens)
        char_lengths = [len(token_chars) for token_chars in characters]
        return characters, token_lengths, char_lengths

    def _numberize_token(self, token):
        return [ord(c) for c in token.value]

    def tensorize(self, batch):
        characters, token_lengths, char_lengths = zip(*batch)
        return (
            pad_and_tensorize(characters),
            pad_and_tensorize(token_lengths),
            pad_and_tensorize(char_lengths),
        )

    def sort_key(self, row):
        return len(row[0])


class LabelTensorizer(Tensorizer):
    """Numberize labels. Label can be used as either input or target """

    __EXPANSIBLE__ = True

    class Config(Tensorizer.Config):
        #: The name of the label column to parse from the data source.
        column: str = "label"
        #: Whether to allow for unknown labels at test/prediction time
        allow_unknown: bool = False
        #: if vocab should have pad, usually false when label is used as target
        pad_in_vocab: bool = False
        #: The label values, if known. Will skip initialization step if provided.
        label_vocab: Optional[List[str]] = None

    is_input = False

    @classmethod
    def from_config(cls, config: Config):
        return cls(
            config.column, config.allow_unknown, config.pad_in_vocab, config.label_vocab
        )

    def __init__(
        self,
        label_column: str = "label",
        allow_unknown: bool = False,
        pad_in_vocab: bool = False,
        label_vocab: Optional[List[str]] = None,
    ):
        self.label_column = label_column
        self.pad_in_vocab = pad_in_vocab
        self.vocab_builder = VocabBuilder()
        self.vocab_builder.use_pad = pad_in_vocab
        self.vocab_builder.use_unk = allow_unknown
        self.vocab = None
        self.pad_idx = -1
        if label_vocab:
            self.vocab_builder.add_all(label_vocab)
            self.vocab, self.pad_idx = self._create_vocab()

    @property
    def column_schema(self):
        return [(self.label_column, str)]

    def initialize(self):
        """
        Look through the dataset for all labels and create a vocab map for them.
        """
        if self.vocab:
            return
        try:
            while True:
                row = yield
                labels = row[self.label_column]
                self.vocab_builder.add_all(labels)
        except GeneratorExit:
            self.vocab, self.pad_idx = self._create_vocab()

    def _create_vocab(self):
        vocab = self.vocab_builder.make_vocab()
        pad_idx = (
            vocab.get_pad_index()
            if self.pad_in_vocab
            else Padding.DEFAULT_LABEL_PAD_IDX
        )
        return vocab, pad_idx

    def numberize(self, row):
        """Numberize labels."""
        return self.vocab.lookup_all(row[self.label_column])

    def tensorize(self, batch):
        return pad_and_tensorize(batch, self.pad_idx)


class LabelListTensorizer(LabelTensorizer):
    """LabelListTensorizer takes a list of labels as input and generate a tuple
    of tensors (label_idx, list_length).
    """

    def __init__(self, label_column: str = "label", *args, **kwargs):
        super().__init__(label_column, *args, **kwargs)

    @property
    def column_schema(self):
        return [(self.label_column, List[str])]

    def numberize(self, row):
        labels = super().numberize(row)
        return labels, len(labels)

    def tensorize(self, batch):
        labels, labels_len = zip(*batch)
        return super().tensorize(labels), pad_and_tensorize(labels_len)

    def sort_key(self, row):
        # use list length as sort key
        return row[1]


class UidTensorizer(Tensorizer):
    """Numberize user IDs which can be either strings or tensors."""

    class Config(Tensorizer.Config):
        column: str = "uid"
        # Allow unknown users during prediction.
        allow_unknown: bool = True

    @classmethod
    def from_config(cls, config: Config):
        return cls(config.column, config.allow_unknown)

    def __init__(self, uid_column: str = "uid", allow_unknown: bool = True):
        self.uid_column = uid_column
        self.vocab_builder = VocabBuilder()
        # User IDs should have the same lengths so need not to use padding.
        self.vocab_builder.use_pad = False
        self.vocab_builder.use_unk = allow_unknown
        self.vocab = None
        self.pad_idx = -1

    @property
    def column_schema(self):
        return [(self.uid_column, str)]

    def _get_row_value_as_str(self, row) -> str:
        """Handle the case that the row value is not a string."""
        row_value = row[self.uid_column]
        if isinstance(row_value, torch.Tensor):
            assert (
                row_value.dim() == 0 or len(row_value) == 1
            ), "Cannot get the value of multi-dimensional tensors."
            row_value = str(row_value.item())
        return row_value

    def initialize(self):
        """
        Look through the dataset for all uids and create a vocab map for them.
        """
        if self.vocab:
            return
        try:
            while True:
                row = yield
                uids = self._get_row_value_as_str(row)
                self.vocab_builder.add_all(uids)
        except GeneratorExit:
            self.vocab, self.pad_idx = self._create_vocab()

    def _create_vocab(self):
        vocab = self.vocab_builder.make_vocab()
        pad_idx = Padding.DEFAULT_LABEL_PAD_IDX
        return vocab, pad_idx

    def numberize(self, row):
        """Numberize uids."""
        return self.vocab.lookup_all(self._get_row_value_as_str(row))

    def tensorize(self, batch):
        return pad_and_tensorize(batch, self.pad_idx)


class SoftLabelTensorizer(LabelTensorizer):
    """
    Handles numberizing labels for knowledge distillation. This still requires the same
    label column as `LabelTensorizer` for the "true" label, but also processes soft
    "probabilistic" labels generated from a teacher model, via three new columns.
    """

    class Config(LabelTensorizer.Config):
        probs_column: str = "target_probs"
        logits_column: str = "target_logits"
        labels_column: str = "target_labels"

    @classmethod
    def from_config(cls, config: Config):
        return cls(
            config.column,
            config.allow_unknown,
            config.pad_in_vocab,
            config.label_vocab,
            config.probs_column,
            config.logits_column,
            config.labels_column,
        )

    def __init__(
        self,
        label_column: str = "label",
        allow_unknown: bool = False,
        pad_in_vocab: bool = False,
        label_vocab: Optional[List[str]] = None,
        probs_column: str = "target_probs",
        logits_column: str = "target_logits",
        labels_column: str = "target_labels",
    ):
        super().__init__(label_column, allow_unknown, pad_in_vocab, label_vocab)
        self.probs_column = probs_column
        self.logits_column = logits_column
        self.labels_column = labels_column

    @property
    def column_schema(self):
        return [
            (self.label_column, str),
            (self.probs_column, List[float]),
            (self.logits_column, List[float]),
            (self.labels_column, List[str]),
        ]

    def numberize(self, row):
        """Numberize hard and soft labels"""
        label = self.vocab.lookup_all(row[self.label_column])
        row_labels = row[self.labels_column]
        probs = align_target_label(row[self.probs_column], row_labels, self.vocab.idx)
        logits = align_target_label(row[self.logits_column], row_labels, self.vocab.idx)
        return label, probs, logits

    def tensorize(self, batch):
        label, probs, logits = zip(*batch)
        return (
            pad_and_tensorize(label, self.pad_idx),
            pad_and_tensorize(probs, dtype=torch.float),
            pad_and_tensorize(logits, dtype=torch.float),
        )


class NumericLabelTensorizer(Tensorizer):
    """Numberize numeric labels."""

    class Config(Tensorizer.Config):
        #: The name of the label column to parse from the data source.
        column: str = "label"
        #: If provided, the range of values the raw label can be. Will rescale the
        #: label values to be within [0, 1].
        rescale_range: Optional[List[float]] = None

    is_input = False

    @classmethod
    def from_config(cls, config: Config):
        return cls(config.column, config.rescale_range)

    def __init__(
        self,
        label_column: str = Config.column,
        rescale_range: Optional[List[float]] = Config.rescale_range,
    ):
        self.label_column = label_column
        if rescale_range is not None:
            assert len(rescale_range) == 2
            assert rescale_range[0] < rescale_range[1]
        self.rescale_range = rescale_range

    @property
    def column_schema(self):
        return [(self.label_column, str)]

    def numberize(self, row):
        """Numberize labels."""
        label = float(row[self.label_column])
        if self.rescale_range is not None:
            label -= self.rescale_range[0]
            label /= self.rescale_range[1] - self.rescale_range[0]
            assert 0 <= label <= 1
        return label

    def tensorize(self, batch):
        return pad_and_tensorize(batch, dtype=torch.float)


class FloatListTensorizer(Tensorizer):
    """Numberize numeric labels."""

    class Config(Tensorizer.Config):
        #: The name of the label column to parse from the data source.
        column: str
        error_check: bool = False
        dim: Optional[int] = None
        # If you wish to normalize the training data here, you probably also
        # want to normalize the inference data. This is currently supported with
        # TorchScript models (see DocModel). See T48207828 for progress on
        # supporting Caffe2 models.
        normalize: bool = False

    @classmethod
    def from_config(cls, config: Config):
        return cls(config.column, config.error_check, config.dim, config.normalize)

    def __init__(
        self, column: str, error_check: bool, dim: Optional[int], normalize: bool
    ):
        self.column = column
        self.error_check = error_check
        self.dim = dim
        self.normalizer = utils_torch.VectorNormalizer(dim, normalize)
        assert not self.error_check or self.dim is not None, "Error check requires dim"

    @property
    def column_schema(self):
        return [(self.column, List[float])]

    def initialize(self):
        try:
            while True:
                row = yield
                res = row[self.column]
                self.normalizer.update_meta_data(res)
        except GeneratorExit:
            self.normalizer.calculate_feature_stats()

    def numberize(self, row):
        dense = row[self.column]
        if self.error_check:
            assert (
                len(dense) == self.dim
            ), f"Dense feature didn't match expected dimension {self.dim}: {dense}"
        return self.normalizer.normalize([dense])[0]

    def tensorize(self, batch):
        return pad_and_tensorize(batch, dtype=torch.float)


NO_LABEL = SpecialToken("NoLabel")


class SlotLabelTensorizer(Tensorizer):
    """Numberize word/slot labels."""

    class Config(Tensorizer.Config):
        #: The name of the slot label column to parse from the data source.
        slot_column: str = "slots"
        #: The name of the text column to parse from the data source.
        #: We need this to be able to generate tensors which correspond to input text.
        text_column: str = "text"
        #: The tokenizer to use to split input text into tokens. This should be
        #: configured in a way which yields tokens consistent with the tokens input to
        #: or output by a model, so that the labels generated by this tensorizer
        #: will match the indices of the model's tokens.
        tokenizer: Tokenizer.Config = Tokenizer.Config()
        #: Whether to allow for unknown labels at test/prediction time
        allow_unknown: bool = False

    is_input = False

    @classmethod
    def from_config(cls, config: Component.Config):
        tokenizer = create_component(ComponentType.TOKENIZER, config.tokenizer)
        return cls(
            config.slot_column, config.text_column, tokenizer, config.allow_unknown
        )

    def __init__(
        self,
        slot_column: str = Config.slot_column,
        text_column: str = Config.text_column,
        tokenizer: Tokenizer = None,
        allow_unknown: bool = Config.allow_unknown,
    ):
        self.slot_column = slot_column
        self.text_column = text_column
        self.allow_unknown = allow_unknown
        self.tokenizer = tokenizer or Tokenizer()
        self.pad_idx = Padding.DEFAULT_LABEL_PAD_IDX

    @property
    def column_schema(self):
        return [(self.text_column, str), (self.slot_column, List[Slot])]

    def initialize(self):
        """Look through the dataset for all labels and create a vocab map for them."""
        builder = VocabBuilder()
        builder.add(NO_LABEL)
        builder.use_pad = False
        builder.use_unk = self.allow_unknown
        try:
            while True:
                row = yield
                slots = row[self.slot_column]
                builder.add_all(s.label for s in slots)
        except GeneratorExit:
            self.vocab = builder.make_vocab()

    def numberize(self, row):
        """
        Turn slot labels and text into a list of token labels with the same
        length as the number of tokens in the text.
        """
        slots = row[self.slot_column]
        text = row[self.text_column]
        tokens = self.tokenizer.tokenize(text)
        indexed_tokens = tokens
        labels = []
        current_slot = 0
        current_token = 0
        while current_token < len(tokens) and current_slot < len(slots):
            _, start, end = indexed_tokens[current_token]
            slot = slots[current_slot]
            if start > slot.end:
                current_slot += 1
            else:
                current_token += 1
                labels.append(slot.label if end > slot.start else NO_LABEL)
        labels += [NO_LABEL] * (len(tokens) - current_token)
        return self.vocab.lookup_all(labels)

    def tensorize(self, batch):
        return pad_and_tensorize(batch, dtype=torch.long)


class SlotLabelTensorizerExpansible(SlotLabelTensorizer):
    """Create a base SlotLabelTensorizer to support selecting different
       types in ModelInput."""

    __EXPANSIBLE__ = True


class GazetteerTensorizer(Tensorizer):
    """
    Create 3 tensors for dict features.

    - idx: index of feature in token order.
    - weights: weight of feature in token order.
    - lens: number of features per token.

    For each input token, there will be the same number of `idx` and `weights` entries.
    (equal to the max number of features any token has in this row). The values
    in `lens` will tell how many of these features are actually used per token.

    Input format for the dict column is json and should be a list of dictionaries
    containing the "features" and their weight for each relevant "tokenIdx". Example:
    ::

        text: "Order coffee from Starbucks please"
        dict: [
            {"tokenIdx": 1, "features": {"drink/beverage": 0.8, "music/song": 0.2}},
            {"tokenIdx": 3, "features": {"store/coffee_shop": 1.0}}
        ]

    if we assume this vocab
    ::

        vocab = {
            UNK: 0, PAD: 1,
            "drink/beverage": 2, "music/song": 3, "store/coffee_shop": 4
        }

    this example will result in those tensors:
    ::

        idx =     [1,   1,   2,   3,   1,   1,   4,   1,   1,   1]
        weights = [0.0, 0.0, 0.8, 0.2, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
        lens =    [1,        2,        1,        1,        1]

    """

    class Config(Tensorizer.Config):
        text_column: str = "text"
        dict_column: str = "dict"
        #: tokenizer to split text and create dict tensors of the same size.
        tokenizer: Tokenizer.Config = Tokenizer.Config()

    @classmethod
    def from_config(cls, config: Config):
        tokenizer = create_component(ComponentType.TOKENIZER, config.tokenizer)
        return cls(config.text_column, config.dict_column, tokenizer)

    def __init__(
        self,
        text_column: str = Config.text_column,
        dict_column: str = Config.dict_column,
        tokenizer: Tokenizer = None,
    ):
        self.text_column = text_column
        self.dict_column = dict_column
        self.tokenizer = tokenizer or Tokenizer()

    @property
    def column_schema(self):
        return [(self.text_column, str), (self.dict_column, Gazetteer)]

    def initialize(self):
        """
        Look through the dataset for all dict features to create vocab.
        """
        builder = VocabBuilder()
        try:
            while True:
                row = yield
                for token_dict in row[self.dict_column]:
                    builder.add_all(token_dict["features"])
        except GeneratorExit:
            self.vocab = builder.make_vocab()

    def numberize(self, row):
        """
        Numberize dict features. Fill in for tokens with no features with
        PAD and weight 0.0. All tokens need to have at least one entry.
        Tokens with more than one feature will have multiple idx and weight
        added in sequence.
        """

        num_tokens = len(self.tokenizer.tokenize(row[self.text_column]))
        num_labels = max(len(t["features"]) for t in row[self.dict_column])
        res_idx = [self.vocab.get_pad_index()] * (num_labels * num_tokens)
        res_weights = [0.0] * (num_labels * num_tokens)
        res_lens = [1] * num_tokens
        for dict_feature in row[self.dict_column]:
            idx = dict_feature["tokenIdx"]
            feats = dict_feature["features"]
            pos = idx * num_labels
            res_lens[idx] = len(feats)
            # write values at the correct pos
            for label, weight in feats.items():
                res_idx[pos] = self.vocab.lookup_all(label)
                res_weights[pos] = weight
                pos += 1

        return res_idx, res_weights, res_lens

    def tensorize(self, batch):
        # Pad a minibatch of dictionary features to be
        # batch_size * max_number_of_words * max_number_of_features
        # unpack the minibatch
        feats, weights, lengths = zip(*batch)
        lengths_flattened = [l for l_list in lengths for l in l_list]
        seq_lens = [len(l_list) for l_list in lengths]
        max_ex_len = max(seq_lens)
        max_feat_len = max(lengths_flattened)
        all_lengths, all_feats, all_weights = [], [], []
        for i, seq_len in enumerate(seq_lens):
            ex_feats, ex_weights, ex_lengths = [], [], []
            feats_lengths, feats_vals, feats_weights = lengths[i], feats[i], weights[i]
            max_feat_len_example = max(feats_lengths)
            r_offset = 0
            for _ in feats_lengths:
                # The dict feats obtained from the featurizer will have necessary
                # padding at the utterance level. Therefore we move the offset by
                # max feature length in the example.
                ex_feats.extend(feats_vals[r_offset : r_offset + max_feat_len_example])
                ex_feats.extend(
                    [self.vocab.get_pad_index()] * (max_feat_len - max_feat_len_example)
                )
                ex_weights.extend(
                    feats_weights[r_offset : r_offset + max_feat_len_example]
                )
                ex_weights.extend([0.0] * (max_feat_len - max_feat_len_example))
                r_offset += max_feat_len_example
            ex_lengths.extend(feats_lengths)
            # Pad examples
            ex_padding = (max_ex_len - seq_len) * max_feat_len
            ex_feats.extend([self.vocab.get_pad_index()] * ex_padding)
            ex_weights.extend([0.0] * ex_padding)
            ex_lengths.extend([1] * (max_ex_len - seq_len))
            all_feats.append(ex_feats)
            all_weights.append(ex_weights)
            all_lengths.append(ex_lengths)
        return (
            cuda.tensor(all_feats, torch.long),
            cuda.tensor(all_weights, torch.float),
            cuda.tensor(all_lengths, torch.long),
        )


class SeqTokenTensorizer(Tensorizer):
    """
    Tensorize a sequence of sentences. The input is a list of strings,
    like this one:
    ::

        ["where do you wanna meet?", "MPK"]

    if we assume this vocab
    ::

        vocab  {
          UNK: 0, PAD: 1,
          'where': 2, 'do': 3, 'you': 4, 'wanna': 5, 'meet?': 6, 'mpk': 7
        }

    this example will result in those tensors:
    ::

        idx = [[2, 3, 4, 5, 6], [7, 1, 1, 1, 1]]
        seq_len = [2]

    If you're using BOS, EOS, BOL and EOL, the vocab will look like this
    ::

        vocab  {
          UNK: 0, PAD: 1,  BOS: 2, EOS: 3, BOL: 4, EOL: 5
          'where': 6, 'do': 7, 'you': 8, 'wanna': 9, 'meet?': 10, 'mpk': 11
        }

    this example will result in those tensors:
    ::

        idx = [
            [2,  4, 3, 1, 1,  1, 1],
            [2,  6, 7, 8, 9, 10, 3],
            [2, 11, 3, 1, 1,  1, 1],
            [2,  5, 3, 1, 1,  1, 1]
        ]
        seq_len = [4]

    """

    class Config(Tensorizer.Config):
        column: str = "text_seq"
        max_seq_len: Optional[int] = None
        #: sentence markers
        add_bos_token: bool = False
        add_eos_token: bool = False
        use_eos_token_for_bos: bool = False
        #: list markers
        add_bol_token: bool = False
        add_eol_token: bool = False
        use_eol_token_for_bol: bool = False
        #: The tokenizer to use to split input text into tokens.
        tokenizer: Tokenizer.Config = Tokenizer.Config()

    @classmethod
    def from_config(cls, config: Config):
        tokenizer = create_component(ComponentType.TOKENIZER, config.tokenizer)
        return cls(
            column=config.column,
            tokenizer=tokenizer,
            add_bos_token=config.add_bos_token,
            add_eos_token=config.add_eos_token,
            use_eos_token_for_bos=config.use_eos_token_for_bos,
            add_bol_token=config.add_bol_token,
            add_eol_token=config.add_eol_token,
            use_eol_token_for_bol=config.use_eol_token_for_bol,
            max_seq_len=config.max_seq_len,
        )

    def __init__(
        self,
        column: str = Config.column,
        tokenizer=None,
        add_bos_token: bool = Config.add_bos_token,
        add_eos_token: bool = Config.add_eos_token,
        use_eos_token_for_bos: bool = Config.use_eos_token_for_bos,
        add_bol_token: bool = Config.add_bol_token,
        add_eol_token: bool = Config.add_eol_token,
        use_eol_token_for_bol: bool = Config.use_eol_token_for_bol,
        max_seq_len=Config.max_seq_len,
        vocab=None,
    ):
        self.column = column
        self.tokenizer = tokenizer or Tokenizer()
        self.vocab = vocab
        self.add_bos_token = add_bos_token
        self.add_eos_token = add_eos_token
        self.use_eos_token_for_bos = use_eos_token_for_bos
        self.add_bol_token = add_bol_token
        self.add_eol_token = add_eol_token
        self.use_eol_token_for_bol = use_eol_token_for_bol
        self.max_seq_len = max_seq_len or 2 ** 30  # large number

    @property
    def column_schema(self):
        return [(self.column, List[str])]

    def initialize(self, vocab_builder=None):
        """Build vocabulary based on training corpus."""
        if self.vocab:
            return
        vocab_builder = vocab_builder or VocabBuilder()
        vocab_builder.use_bos = self.add_bos_token
        vocab_builder.use_eos = self.add_eos_token
        vocab_builder.use_bol = self.add_bol_token
        vocab_builder.use_eol = self.add_eol_token

        try:
            while True:
                row = yield
                for raw_text in row[self.column]:
                    tokenized = self.tokenizer.tokenize(raw_text)
                    vocab_builder.add_all([t.value for t in tokenized])
        except GeneratorExit:
            self.vocab = vocab_builder.make_vocab()

    _lookup_tokens = TokenTensorizer._lookup_tokens
    _tokenize = TokenTensorizer._tokenize

    def numberize(self, row):
        """Tokenize, look up in vocabulary."""
        seq = []

        if self.add_bol_token:
            bol = EOL if self.use_eol_token_for_bol else BOL
            tokens, _, _ = self._lookup_tokens(pre_tokenized=[Token(bol, -1, -1)])
            seq.append(tokens)

        for raw_text in row[self.column]:
            tokens, _, _ = self._lookup_tokens(raw_text)
            seq.append(tokens)

        if self.add_eol_token:
            tokens, _, _ = self._lookup_tokens(pre_tokenized=[Token(EOL, -1, -1)])
            seq.append(tokens)

        max_len = max(len(sentence) for sentence in seq)
        for sentence in seq:
            pad_len = max_len - len(sentence)
            if pad_len:
                sentence += [self.vocab.get_pad_index()] * pad_len
        return seq, len(seq)

    def tensorize(self, batch):
        tokens, seq_lens = zip(*batch)
        return (
            pad_and_tensorize(tokens, self.vocab.get_pad_index()),
            pad_and_tensorize(seq_lens),
        )

    def sort_key(self, row):
        # use seq_len as sort key
        return row[1]


class AnnotationNumberizer(Tensorizer):
    """
    Not really a Tensorizer (since it does not create tensors) but technically
    serves the same function. This class parses Annotations in the format below
    and extracts the actions (type List[List[int]])
    ::

        [IN:GET_ESTIMATED_DURATION How long will it take to [SL:METHOD_TRAVEL
        drive ] from [SL:SOURCE Chicago ] to [SL:DESTINATION Mississippi ] ]

    Extraction algorithm is handled by Annotation class. We only care about
    the list of actions, which before vocab index lookups would look like:
    ::

        [
            IN:GET_ESTIMATED_DURATION, SHIFT, SHIFT, SHIFT, SHIFT, SHIFT, SHIFT,
            SL:METHOD_TRAVEL, SHIFT, REDUCE,
            SHIFT,
            SL:SOURCE, SHIFT, REDUCE,
            SHIFT,
            SL:DESTINATION, SHIFT, REDUCE,
        ]

    """

    class Config(Tensorizer.Config):
        column: str = "seqlogical"

    @classmethod
    def from_config(cls, config: Config):
        return cls(column=config.column)

    def __init__(self, column: str = Config.column, vocab=None):
        self.column = column
        self.vocab = vocab

    @property
    def column_schema(self):
        return [(self.column, List[str])]

    def initialize(self, vocab_builder=None):
        """Build vocabulary based on training corpus."""
        if self.vocab:
            return
        vocab_builder = vocab_builder or VocabBuilder()
        vocab_builder.use_unk = False
        vocab_builder.use_pad = False

        try:
            while True:
                row = yield
                annotation = Annotation(row[self.column])
                actions = annotation.tree.to_actions()
                vocab_builder.add_all(actions)
        except GeneratorExit:
            self.vocab = vocab_builder.make_vocab()
            self.shift_idx = self.vocab.idx[SHIFT]
            self.reduce_idx = self.vocab.idx[REDUCE]

            def filterVocab(fn):
                return [token for nt, token in self.vocab.idx.items() if fn(nt)]

            self.ignore_subNTs_roots = filterVocab(is_unsupported)
            self.valid_NT_idxs = filterVocab(is_valid_nonterminal)
            self.valid_IN_idxs = filterVocab(is_intent_nonterminal)
            self.valid_SL_idxs = filterVocab(is_slot_nonterminal)

    def numberize(self, row):
        """Tokenize, look up in vocabulary."""
        annotation = Annotation(row[self.column])
        return self.vocab.lookup_all(annotation.tree.to_actions())

    def tensorize(self, batch):
        return batch


class MetricTensorizer(Tensorizer):
    """A tensorizer which use other tensorizers' numerized data.
       Used mostly for metric reporting."""

    class Config(Tensorizer.Config):
        names: List[str]
        indexes: List[int]

    is_input = False

    @classmethod
    def from_config(cls, config: Config):
        return cls(config.names, config.indexes)

    def __init__(self, names: List[str], indexes: List[int]):
        self.names = names
        self.indexes = indexes

    def numberize(self, row):
        # metric tensorizer will depends on other tensorizers' numeric result
        return None

    def tensorize(self, batch):
        raise NotImplementedError


class NtokensTensorizer(MetricTensorizer):
    """A tensorizer which will reference another tensorizer's numerized data
       to calculate the num tokens.
       Used for calculating tokens per second."""

    def tensorize(self, batch):
        ntokens = 0
        for name, index in zip(self.names, self.indexes):
            ntokens += sum((sample[index] for sample in batch[name]))
        return ntokens


class FloatTensorizer(Tensorizer):
    """A tensorizer for reading in scalars from the data."""

    class Config(Tensorizer.Config):
        #: The name of the column to parse from the data source.
        column: str

    @classmethod
    def from_config(cls, config: Config):
        return cls(config.column)

    def __init__(self, column: str):
        self.column = column

    @property
    def column_schema(self):
        return [(self.column, float)]

    def numberize(self, row):
        return row[self.column]

    def tensorize(self, batch):
        return cuda.tensor(batch, torch.float)


def initialize_tensorizers(tensorizers, data_source):
    """A utility function to stream a data source to the initialize functions
    of a dict of tensorizers."""
    initializers = []
    for init in [tensorizer.initialize() for tensorizer in tensorizers.values()]:
        try:
            init.send(None)  # kick
            initializers.append(init)
        except StopIteration:
            pass

    if initializers:
        for row in data_source:
            for init in initializers:
                init.send(row)
