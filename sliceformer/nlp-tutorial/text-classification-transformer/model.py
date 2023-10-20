import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sinkhorn import SinkhornDistance
from swd import SWD5, SWD7
import time

class ScaledDotProductAttention(nn.Module):
    def __init__(self, d_k, attn='trans'):
        super(ScaledDotProductAttention, self).__init__()
        self.d_k = d_k
        self.swd = SWD7()
        self.attn = attn

    def forward(self, q, k, v, attn_mask, n_it=1, stage='train'):
        # |q| : (batch_size, n_heads, q_len, d_k), |k| : (batch_size, n_heads, k_len, d_k), |v| : (batch_size, n_heads, v_len, d_v)
        # |attn_mask| : (batch_size, n_heads, seq_len(=q_len))

        if self.attn == 'trans':
            attn_score = torch.matmul(q, k.transpose(-1, -2)) / np.sqrt(self.d_k)
            attn_score.masked_fill_(attn_mask, -1e9)
            attn_weights = nn.Softmax(dim=-1)(attn_score)
            attn_weights = attn_weights * attn_weights.shape[-1]
            output = torch.matmul(attn_weights, v)
        elif self.attn == 'sink':
            attn_score = torch.matmul(q, k.transpose(-1, -2)) / np.sqrt(self.d_k)
            attn_score.masked_fill_(attn_mask, -1e9)
            attn_score_shape = attn_score.shape
            # |attn_score| : (batch_size, n_heads, q_len, k_len)
            attn_score = attn_score.view(-1, attn_score_shape[2], attn_score_shape[3])
            sink = SinkhornDistance(1, max_iter=n_it)
            attn_weights = sink(attn_score)[0]
            attn_weights = attn_weights * attn_weights.shape[-1]
            # |attn_weights| : (batch_size, n_heads, q_len, k_len)
            attn_weights = attn_weights.view(attn_score_shape)
            output = torch.matmul(attn_weights, v)
        elif self.attn == 'swd':
            output, attn_weights = self.swd(q, k, v, attn_mask, stage=stage)

        return output, attn_weights

class MultiHeadAttention(nn.Module):
    def __init__(self, d_model, n_heads, n_it=1, print_attention=False, attn='trans'):
        super(MultiHeadAttention, self).__init__()
        self.n_heads = n_heads
        self.d_k = self.d_v = d_model//n_heads

        self.WQ = nn.Linear(d_model, d_model)
        self.WK = nn.Linear(d_model, d_model)
        self.WV = nn.Linear(d_model, d_model)
        self.n_it = n_it
        self.scaled_dot_product_attn = ScaledDotProductAttention(self.d_k, attn=attn)
        self.linear = nn.Linear(n_heads * self.d_v, d_model)
        self.print_attention = print_attention
        
    def forward(self, Q, K, V, attn_mask, stage):
        # |Q| : (batch_size, q_len, d_model), |K| : (batch_size, k_len, d_model), |V| : (batch_size, v_len, d_model)
        # |attn_mask| : (batch_size, seq_len(=q_len), seq_len(=k_len))
        batch_size = Q.size(0)

        q_heads = self.WQ(Q).view(batch_size, -1, self.n_heads, self.d_k).transpose(1, 2)
        k_heads = self.WK(K).view(batch_size, -1, self.n_heads, self.d_k).transpose(1, 2)
        v_heads = self.WV(V).view(batch_size, -1, self.n_heads, self.d_v).transpose(1, 2)
        # |q_heads| : (batch_size, n_heads, q_len, d_k), |k_heads| : (batch_size, n_heads, k_len, d_k), |v_heads| : (batch_size, n_heads, v_len, d_v)

        attn_mask = attn_mask.unsqueeze(1).repeat(1, self.n_heads, 1, 1)
        # attn_mask = attn_mask.unsqueeze(1).repeat(1, self.n_heads, 1)
        # |attn_mask| : (batch_size, n_heads, seq_len(=q_len))
        attn, attn_weights = self.scaled_dot_product_attn(q_heads, k_heads, v_heads, attn_mask, n_it=self.n_it, stage=stage)
        # |attn| : (batch_size, n_heads, q_len, d_v)
        # |attn_weights| : (batch_size, n_heads, q_len, k_len)
        if self.print_attention:
            print('sum over columns of the attention matrix: ', attn_weights.sum(-2))
        attn = attn.transpose(1, 2).contiguous().view(batch_size, -1, self.n_heads * self.d_v)
        # |attn| : (batch_size, q_len, n_heads * d_v)
        output = self.linear(attn)
        # |output| : (batch_size, q_len, d_model)

        return output, attn_weights

class PositionWiseFeedForwardNetwork(nn.Module):
    def __init__(self, d_model, d_ff):
        super(PositionWiseFeedForwardNetwork, self).__init__()

        self.linear1 = nn.Linear(d_model, d_ff)
        self.linear2 = nn.Linear(d_ff, d_model)
        self.relu = nn.ReLU()

    def forward(self, inputs):
        # |inputs| : (batch_size, seq_len, d_model)

        output = self.relu(self.linear1(inputs))
        # |output| : (batch_size, seq_len, d_ff)
        output = self.linear2(output)
        # |output| : (batch_size, seq_len, d_model)

        return output

class EncoderLayer(nn.Module):
    def __init__(self, d_model, n_heads, p_drop, d_ff, n_it=1, print_attention=False, attn='trans'):
        super(EncoderLayer, self).__init__()

        self.mha = MultiHeadAttention(d_model, n_heads, n_it=n_it, print_attention=print_attention, attn=attn)
        self.dropout1 = nn.Dropout(p_drop)
        self.layernorm1 = nn.LayerNorm(d_model, eps=1e-6)
        
        self.ffn = PositionWiseFeedForwardNetwork(d_model, d_ff)
        self.dropout2 = nn.Dropout(p_drop)
        self.layernorm2 = nn.LayerNorm(d_model, eps=1e-6)

    def forward(self, inputs, attn_mask, stage):
        # |inputs| : (batch_size, seq_len, d_model)
        # |attn_mask| : (batch_size, seq_len, seq_len)
        
        attn_outputs, attn_weights = self.mha(inputs, inputs, inputs, attn_mask, stage=stage)
        attn_outputs = self.dropout1(attn_outputs)
        attn_outputs = self.layernorm1(inputs + attn_outputs)
        # |attn_outputs| : (batch_size, seq_len(=q_len), d_model)
        # |attn_weights| : (batch_size, n_heads, q_len, k_len)

        ffn_outputs = self.ffn(attn_outputs)
        ffn_outputs = self.dropout2(ffn_outputs)
        ffn_outputs = self.layernorm2(attn_outputs + ffn_outputs)
        # |ffn_outputs| : (batch_size, seq_len, d_model)
        
        return ffn_outputs, attn_weights

class TransformerEncoder(nn.Module):
    """TransformerEncoder is a stack of N encoder layers.

    Args:
        vocab_size (int)    : vocabulary size (vocabulary: collection mapping token to numerical identifiers)
        seq_len    (int)    : input sequence length
        d_model    (int)    : number of expected features in the input
        n_layers   (int)    : number of sub-encoder-layers in the encoder
        n_heads    (int)    : number of heads in the multiheadattention models
        p_drop     (float)  : dropout value
        d_ff       (int)    : dimension of the feedforward network model
        pad_id     (int)    : pad token id

    Examples:
    >>> encoder = TransformerEncoder(vocab_size=1000, seq_len=512)
    >>> inp = torch.arange(512).repeat(2, 1)
    >>> encoder(inp)
    """
    
    def __init__(self, vocab_size, seq_len, d_model=512, n_layers=6, n_heads=8, p_drop=0.1, d_ff=2048, pad_id=0,
                 n_it=1, print_attention=False, stage='train', attn='trans'):
        super(TransformerEncoder, self).__init__()
        self.pad_id = pad_id
        self.sinusoid_table = self.get_sinusoid_table(seq_len+1, d_model) # (seq_len+1, d_model)

        # layers
        self.embedding = nn.Embedding(vocab_size, d_model)
        self.pos_embedding = nn.Embedding.from_pretrained(self.sinusoid_table, freeze=True)
        self.layers = nn.ModuleList([EncoderLayer(d_model, n_heads, p_drop, d_ff, n_it=n_it,
                                                  print_attention=print_attention, attn=attn) for _ in range(n_layers)])
        # layers to classify
        self.linear = nn.Linear(d_model, 2)
        self.softmax = nn.Softmax(dim=-1)
        self.d_model = d_model
        self.dropout = nn.Dropout(p_drop)

    def forward(self, inputs, stage='train'):
        # |inputs| : (batch_size, seq_len)
        positions = torch.arange(inputs.size(1), device=inputs.device, dtype=inputs.dtype).repeat(inputs.size(0), 1) + 1
        position_pad_mask = inputs.eq(self.pad_id)
        positions.masked_fill_(position_pad_mask, 0)
        # |positions| : (batch_size, seq_len)

        outputs = self.embedding(inputs) + self.pos_embedding(positions)
        # |outputs| : (batch_size, seq_len, d_model)

        attn_pad_mask = self.get_attention_padding_mask(inputs, inputs, self.pad_id)
        # |attn_pad_mask| : (batch_size, seq_len)

        # outputs = self.dropout(outputs)
        attention_weights = None
        # start_time = time.time()
        for layer in self.layers:
            outputs, attn_weights = layer(outputs, attn_pad_mask, stage=stage)
            # |outputs| : (batch_size, seq_len, d_model)
            # |attn_weights| : (batch_size, n_heads, seq_len, seq_len)
            if attention_weights is None and attn_weights is not None:
                attention_weights = attn_weights.detach().clone()
        # print('finish one layer in %.4f seconds' % (time.time() - start_time))
        outputs, _ = torch.max(outputs, dim=1)
        # |outputs| : (batch_size, d_model)
        outputs = self.softmax(self.linear(outputs))
        # |outputs| : (batch_size, 2)

        return outputs, attention_weights

    def get_attention_padding_mask(self, q, k, pad_id):
        attn_pad_mask = k.eq(pad_id).unsqueeze(1).repeat(1, q.size(1), 1)
        # |attn_pad_mask| : (batch_size, q_len, k_len)

        # attn_pad_mask = k.eq(pad_id)
        # |attn_pad_mask| : (batch_size, q_len)

        return attn_pad_mask

    def get_sinusoid_table(self, seq_len, d_model):
        def get_angle(pos, i, d_model):
            return pos / np.power(10000, (2 * (i//2)) / d_model)
        
        sinusoid_table = np.zeros((seq_len, d_model))
        for pos in range(seq_len):
            for i in range(d_model):
                if i%2 == 0:
                    sinusoid_table[pos, i] = np.sin(get_angle(pos, i, d_model))
                else:
                    sinusoid_table[pos, i] = np.cos(get_angle(pos, i, d_model))

        return torch.FloatTensor(sinusoid_table)