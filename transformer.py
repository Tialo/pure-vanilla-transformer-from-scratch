import os
import json
import math
from dataclasses import dataclass, asdict

import torch
import torch.nn as nn


@dataclass
class TransformerConfig:
    vocab_size: int = 8192
    n_encoder_layers: int = 6
    n_decoder_layers: int = 6
    n_encoder_heads: int = 8
    n_decoder_heads: int = 8
    embed_size: int = 512
    d_ff: int = 2048
    max_len: int = 4096
    tie_embeddings: bool = True
    post_ln: bool = True
    use_additional_dropout: bool = False
    xavier_initialization: bool = False


def create_causal_mask(seq_len: int):
    """
    Example:
        >>> create_causal_mask(4)
        tensor([[1, 0, 0, 0],
                [1, 1, 0, 0],
                [1, 1, 1, 0],
                [1, 1, 1, 1]], dtype=torch.uint8)
    """
    # (seq_len, seq_len)
    return torch.tril(torch.ones(seq_len, seq_len), diagonal=0).type(torch.uint8)  


def positional_encoding(seq_len: int, embed_size: int):
    pos_vec = torch.arange(seq_len).unsqueeze(1)  # (seq_len, 1)
    i_times_two_vec = torch.arange(0, embed_size, 2)  # (embed_size // 2)
    pos_encoding = torch.empty(seq_len, embed_size)
    div_term = torch.exp(-math.log(10000) * i_times_two_vec / embed_size)
    pos_encoding[:, ::2] = torch.sin(pos_vec * div_term)
    pos_encoding[:, 1::2] = torch.cos(pos_vec * div_term)
    return pos_encoding  # (seq_len, embed_size)


class ScaledDotProductAttention(nn.Module):
    def __init__(self, use_dropout: bool = False):
        super().__init__()
        # was not mentioned in original paper
        p = 0.1 if use_dropout else 0.0
        self.dropout = nn.Dropout(p=p)

    def forward(self, q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, mask=None):
        """
        q - (batch_size, n_heads, seq_len_q, head_dim)
        k - (batch_size, n_heads, seq_len_kv, head_dim)
        v - (batch_size, n_heads, seq_len_kv, head_dim)
        mask - (batch_size, 1, seq_len_q)  in case of mask for encoder self-attention
        or     (batch_size, 1, seq_len_kv) in case of padding mask for encoder-decoder attention
        or     (1, seq_len_kv, seq_len_kv)  in case of decoder causal self-attention

        seq_len_q == seq_len_kv == seq_len_qkv in case of any self-attention
        """
        d_k = k.size(-1)
        k = k.transpose(-1, -2)  # (batch_size, n_heads, head_dim, seq_len_kv)
        attn_weights = q @ k / d_k**0.5  # (batch_size, n_heads, seq_len_q, seq_len_kv)
        if mask is not None:
            mask = mask.unsqueeze(1)
            # (1, 1, seq_len_qkv, seq_len_qkv) in case of any self-attention
            # (batch_size, 1, 1, seq_len) otherwise
            attn_weights = attn_weights.masked_fill(mask == 0, float("-inf"))
        attn_weights = torch.softmax(attn_weights, dim=-1)
        attn_weights = self.dropout(attn_weights)
        return attn_weights @ v  # (batch_size, n_heads, seq_len_q, head_dim)


class MultiHeadAttention(nn.Module):
    def __init__(
        self, n_heads: int, embed_size: int, use_additional_dropout: bool = False
    ):
        super().__init__()
        assert embed_size % n_heads == 0

        self.n_heads = n_heads
        self.embed_size = embed_size
        self.head_dim = embed_size // n_heads

        self.q_weights = nn.Linear(embed_size, embed_size)
        self.k_weights = nn.Linear(embed_size, embed_size)
        self.v_weights = nn.Linear(embed_size, embed_size)
        self.proj = nn.Linear(embed_size, embed_size)

        self.sdpa = ScaledDotProductAttention(use_dropout=use_additional_dropout)

    def forward(
        self,
        q: torch.Tensor,
        k: torch.Tensor,
        v: torch.Tensor,
        mask: torch.Tensor | None = None,
    ):
        """
        q - (batch_size, seq_len_q, embed_size)
        k - (batch_size, seq_len_kv, embed_size)
        v - (batch_size, seq_len_kv, embed_size)
        mask - refer to ScaledDotProductAttention.forward() for details

        seq_len_q == seq_len_kv in case of self-attention
        """
        batch_size = q.size(0)

        q = self.q_weights(q)
        k = self.k_weights(k)
        v = self.v_weights(v)

        # THIS WON'T WORK.
        # q = q.view(batch_size, self.n_heads, q.size(1), self.head_dim)
        # k = k.view(batch_size, self.n_heads, k.size(1), self.head_dim)
        # v = v.view(batch_size, self.n_heads, v.size(1), self.head_dim)

        # (batch_size, n_heads, seq_len_q, head_dim)
        q = q.view(batch_size, q.size(1), self.n_heads, self.head_dim).transpose(1, 2)
        # (batch_size, n_heads, seq_len_kv, head_dim)
        k = k.view(batch_size, k.size(1), self.n_heads, self.head_dim).transpose(1, 2)
        # (batch_size, n_heads, seq_len_kv, head_dim)
        v = v.view(batch_size, v.size(1), self.n_heads, self.head_dim).transpose(1, 2)  

        # (batch_size, n_heads, seq_len_q, head_dim)
        attentions = self.sdpa(q, k, v, mask=mask)  
        # this won't work without .transpose(1, 2).contiguous()
        attentions = (
            attentions.transpose(1, 2)
            .contiguous()
            .view(batch_size, q.size(2), self.embed_size)
        )
        return self.proj(attentions)  # (batch_size, seq_len_q, embed_size)


class FeedForward(nn.Module):
    def __init__(self, d_out: int, d_ff: int, use_dropout: bool = False):
        super().__init__()
        p = 0.1 if use_dropout else 0.0
        self.ff = nn.Sequential(
            nn.Linear(d_out, d_ff),
            nn.ReLU(),
            nn.Dropout(p=p),  # was not mentioned in original paper
            nn.Linear(d_ff, d_out),
        )

    def forward(self, x):
        return self.ff(x)


class EncoderLayer(nn.Module):
    def __init__(
        self,
        n_heads: int,
        embed_size: int,
        d_ff: int,
        post_ln: bool = True,
        use_additional_dropout: bool = False,
    ):
        super().__init__()
        self.ff = FeedForward(embed_size, d_ff, use_dropout=use_additional_dropout)
        self.mha = MultiHeadAttention(
            n_heads, embed_size, use_additional_dropout=use_additional_dropout
        )
        self.ln1 = nn.LayerNorm(embed_size)
        self.ln2 = nn.LayerNorm(embed_size)
        # 5.4 We apply dropout to the output of each sub-layer, before it is added to the sub-layer input and normalized
        self.dropout1 = nn.Dropout(p=0.1)
        self.dropout2 = nn.Dropout(p=0.1)
        self.post_ln = post_ln

    def forward(self, x: torch.Tensor, mask: torch.Tensor | None = None):
        """
        x - (batch_size, seq_len_src, embed_size)
        mask - (batch_size, 1, seq_len_src)
        """
        if self.post_ln:
            attention = self.ln1(x + self.dropout1(self.mha(x, x, x, mask)))
            # (batch_size, seq_len_src, embed_size)
            return self.ln2(attention + self.dropout2(self.ff(attention)))  
        else:
            x_ln = self.ln1(x)
            attention = x + self.dropout1(self.mha(x_ln, x_ln, x_ln, mask))
            attention_ln = self.ln2(attention)
            # (batch_size, seq_len_src, embed_size)
            return attention + self.dropout2(self.ff(attention_ln))  


class Encoder(nn.Module):
    def __init__(
        self,
        n_layers: int,
        n_heads: int,
        embed_size: int,
        d_ff: int,
        post_ln: bool = True,
        use_additional_dropout: bool = False,
    ):
        super().__init__()
        self.encoder_layers = nn.ModuleList(
            [
                EncoderLayer(
                    n_heads,
                    embed_size,
                    d_ff,
                    post_ln=post_ln,
                    use_additional_dropout=use_additional_dropout,
                )
                for _ in range(n_layers)
            ]
        )
        self.final_ln = None if post_ln else nn.LayerNorm(embed_size)

    def forward(self, x: torch.Tensor, mask: torch.Tensor | None = None):
        """
        x - (batch_size, seq_len_src, embed_size)
        mask - (batch_size, 1, seq_len_src)
        """
        for encoder_layer in self.encoder_layers:
            x = encoder_layer(x, mask)  # (batch_size, seq_len_src, embed_size)
        if self.final_ln is not None:
            x = self.final_ln(x)
        return x


class DecoderLayer(nn.Module):
    def __init__(
        self,
        n_heads: int,
        embed_size: int,
        d_ff: int,
        post_ln: bool = True,
        use_additional_dropout: bool = False,
    ):
        super().__init__()
        self.ff = FeedForward(embed_size, d_ff, use_dropout=use_additional_dropout)
        self.mha = MultiHeadAttention(
            n_heads, embed_size, use_additional_dropout=use_additional_dropout
        )
        self.mmha = MultiHeadAttention(
            n_heads, embed_size, use_additional_dropout=use_additional_dropout
        )
        self.ln1 = nn.LayerNorm(embed_size)
        self.ln2 = nn.LayerNorm(embed_size)
        self.ln3 = nn.LayerNorm(embed_size)
        # 5.4 We apply dropout to the output of each sub-layer, before it is added to the sub-layer input and normalized
        self.dropout1 = nn.Dropout(p=0.1)
        self.dropout2 = nn.Dropout(p=0.1)
        self.dropout3 = nn.Dropout(p=0.1)
        self.post_ln = post_ln

    def forward(
        self,
        x: torch.Tensor,
        memory: torch.Tensor,
        causal_mask: torch.Tensor,
        encoder_mask: torch.Tensor | None = None,
    ):
        """
        x - (batch_size, seq_len_tgt, embed_size)
        memory - (batch_size, seq_len_src, embed_size)
        causal_mask - (1, seq_len_tgt, seq_len_tgt)
        encoder_mask - (batch_size, 1, seq_len_src)
        """
        if self.post_ln:
            # (batch_size, seq_len_tgt, embed_size)
            attention = self.ln1(x + self.dropout1(self.mmha(x, x, x, mask=causal_mask)))
            attention = self.ln2(
                attention
                + self.dropout2(self.mha(attention, memory, memory, mask=encoder_mask))
            )
            # (batch_size, seq_len_tgt, embed_size)
            return self.ln3(attention + self.dropout3(self.ff(attention)))  
        else:
            x_ln = self.ln1(x)
            attention = x + self.dropout1(self.mmha(x_ln, x_ln, x_ln, mask=causal_mask))
            attention_ln = self.ln2(attention)
            attention = attention + self.dropout2(self.mha(attention_ln, memory, memory, mask=encoder_mask))
            attention_ln = self.ln3(attention)
            # (batch_size, seq_len_tgt, embed_size)
            return attention + self.dropout3(self.ff(attention_ln))  


class Decoder(nn.Module):
    def __init__(
        self,
        n_layers: int,
        n_heads: int,
        embed_size: int,
        d_ff: int,
        post_ln: bool = True,
        use_additional_dropout: bool = False,
    ):
        super().__init__()
        self.decoder_layers = nn.ModuleList(
            [
                DecoderLayer(
                    n_heads,
                    embed_size,
                    d_ff,
                    post_ln=post_ln,
                    use_additional_dropout=use_additional_dropout,
                )
                for _ in range(n_layers)
            ]
        )
        self.final_ln = None if post_ln else nn.LayerNorm(embed_size)

    def forward(
        self,
        x: torch.Tensor,
        memory: torch.Tensor,
        encoder_mask: torch.Tensor | None = None,
    ):
        """
        x - (batch_size, seq_len_tgt, embed_size)
        memory - (batch_size, seq_len_src, embed_size)
        encoder_mask - (batch_size, 1, seq_len_src)
        """
        tgt_mask = (
            create_causal_mask(
                x.size(1),
            )
            .unsqueeze(0)
            .to(x.device)
        )  # (1, seq_len_tgt, seq_len_tgt)
        for decoder_layer in self.decoder_layers:
            # (batch_size, seq_len_tgt, embed_size)
            x = decoder_layer(x, memory, tgt_mask, encoder_mask=encoder_mask)  
        if self.final_ln is not None:
            x = self.final_ln(x)
        return x


class Transformer(nn.Module):
    def __init__(
        self,
        config: TransformerConfig,
    ):
        super().__init__()
        self.config = config
        # compute positional encoding for max_len once to save time for each forward pass
        self.pos_enc = positional_encoding(config.max_len, config.embed_size)
        self.encoder = Encoder(
            config.n_encoder_layers,
            config.n_encoder_heads,
            config.embed_size,
            config.d_ff,
            post_ln=config.post_ln,
            use_additional_dropout=config.use_additional_dropout,
        )
        self.decoder = Decoder(
            config.n_decoder_layers,
            config.n_decoder_heads,
            config.embed_size,
            config.d_ff,
            post_ln=config.post_ln,
            use_additional_dropout=config.use_additional_dropout,
        )

        self.sqrt_dmodel = config.embed_size**0.5
        # original paper used shared embedding layer for source and target languages
        self.embeddings = nn.Embedding(config.vocab_size, config.embed_size)
        self.fc = nn.Linear(config.embed_size, config.vocab_size)

        # 3.4 In our model, we share the same weight matrix between the two
        # embedding layers and the pre-softmax linear transformation
        # see also https://paperswithcode.com/method/weight-tying
        if config.tie_embeddings:
            self.embeddings.weight = self.fc.weight

        # 5.4 we apply dropout to the sums of the embeddings and the positional encodings
        self.dropout = nn.Dropout(p=0.1)
        if config.xavier_initialization:
            self._init_params()

    def _init_params(self) -> None:
        for _, p in self.named_parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def encode(
        self, src: torch.Tensor, src_mask: torch.Tensor | None = None
    ) -> tuple[torch.Tensor, torch.Tensor | None]:
        """
        src - (batch_size, seq_len_src)
        src_mask - (batch_size, seq_len_src)
        """
        if src_mask is not None:
            src_mask = src_mask[:, torch.newaxis]  # (batch_size, 1, seq_len_src)
        src_embed = self.embeddings(src)  # (batch_size, seq_len_src, embed_size)
        # 3.4 In the embedding layers, we multiply those weights by √dmodel
        src_embed *= self.sqrt_dmodel
        # (batch_size, seq_len_src, embed_size)
        src_embed += self.pos_enc[: src.size(1)].to(src_embed.device)  
        src_embed = self.dropout(src_embed)  # (batch_size, seq_len_src, embed_size)
        # (batch_size, seq_len_src, embed_size)
        memory = self.encoder(src_embed, mask=src_mask)  
        return memory, src_mask

    def decode(
        self,
        memory: torch.Tensor,
        tgt: torch.Tensor,
        src_mask: torch.Tensor | None = None,
    ) -> torch.Tensor:
        """
        memory - (batch_size, seq_len_src, embed_size)
        tgt - (batch_size, seq_len_tgt)
        src_mask - (batch_size, seq_len_src)
        """
        tgt_embed = self.embeddings(tgt)  # (batch_size, seq_len_tgt, embed_size)
        # 3.4 In the embedding layers, we multiply those weights by √dmodel
        tgt_embed *= self.sqrt_dmodel
        tgt_embed += self.pos_enc[: tgt.size(1)].to(tgt_embed.device)
        tgt_embed = self.dropout(tgt_embed)
        # (batch_size, seq_len_tgt, embed_size)
        attention = self.decoder(tgt_embed, memory, encoder_mask=src_mask)  
        return self.fc(attention)  # (batch_size, seq_len_tgt, vocab_size)

    def forward(
        self, src: torch.Tensor, tgt: torch.Tensor, src_mask: torch.Tensor | None = None
    ) -> torch.Tensor:
        """
        src - (batch_size, seq_len_src)
        tgt - (batch_size, seq_len_tgt)
        src_mask - (batch_size, seq_len_src)
        """
        memory, src_mask = self.encode(src, src_mask)
        # (batch_size, seq_len_tgt, vocab_size)
        return self.decode(memory, tgt, src_mask)

    def save_pretrained(self, save_path: str) -> None:
        torch.save(self.state_dict(), os.path.join(save_path, "model.pt"))
        with open(os.path.join(save_path, "config.json"), "w") as f:
            json.dump(asdict(self.config), f, indent=2)

    @classmethod
    def from_pretrained(cls, pretrained_path: str):
        with open(os.path.join(pretrained_path, "config.json")) as f:
            config = json.load(f)
        model = cls(TransformerConfig(**config))
        state_dict = torch.load(os.path.join(pretrained_path, "model.pt"))
        model.load_state_dict(state_dict)
        return model
