# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/models_transformer__informer.ipynb (unless otherwise specified).

__all__ = ['Informer']

# Cell
import random
from fastcore.foundation import patch

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import pytorch_lightning as pl
from torch import optim

from ..components.transformer import Decoder, DecoderLayer, Encoder, EncoderLayer, ConvLayer
from ..components.selfattention import (
    ProbAttention, AttentionLayer
)
from ..components.embed import DataEmbedding
from ...losses.utils import LossFunction
from ...data.tsdataset import IterateWindowsDataset
from ...data.tsloader import TimeSeriesLoader

# Cell
class _Informer(nn.Module):
    """
    Informer with Propspare attention in O(LlogL) complexity
    """
    def __init__(self, pred_len, output_attention,
                 enc_in, dec_in, d_model, c_out, embed, freq, dropout,
                 factor, n_heads, d_ff, activation, e_layers,
                 d_layers, distil):
        super(_Informer, self).__init__()
        self.pred_len = pred_len
        self.output_attention = output_attention

        # Embedding
        self.enc_embedding = DataEmbedding(enc_in, d_model, embed, freq,
                                           dropout)
        self.dec_embedding = DataEmbedding(dec_in, d_model, embed, freq,
                                           dropout)

        # Encoder
        self.encoder = Encoder(
            [
                EncoderLayer(
                    AttentionLayer(
                        ProbAttention(False, factor, attention_dropout=dropout,
                                      output_attention=output_attention),
                        d_model, n_heads),
                    d_model,
                    d_ff,
                    dropout=dropout,
                    activation=activation
                ) for l in range(e_layers)
            ],
            [
                ConvLayer(
                    d_model
                ) for l in range(e_layers - 1)
            ] if distil else None,
            norm_layer=torch.nn.LayerNorm(d_model)
        )
        # Decoder
        self.decoder = Decoder(
            [
                DecoderLayer(
                    AttentionLayer(
                        ProbAttention(True, factor, attention_dropout=dropout, output_attention=False),
                        d_model, n_heads),
                    AttentionLayer(
                        ProbAttention(False, factor, attention_dropout=dropout, output_attention=False),
                        d_model, n_heads),
                    d_model,
                    d_ff,
                    dropout=dropout,
                    activation=activation,
                )
                for l in range(d_layers)
            ],
            norm_layer=torch.nn.LayerNorm(d_model),
            projection=nn.Linear(d_model, c_out, bias=True)
        )

    def forward(self, x_enc, x_mark_enc, x_dec, x_mark_dec,
                enc_self_mask=None, dec_self_mask=None, dec_enc_mask=None):

        enc_out = self.enc_embedding(x_enc, x_mark_enc)
        enc_out, attns = self.encoder(enc_out, attn_mask=enc_self_mask)

        dec_out = self.dec_embedding(x_dec, x_mark_dec)
        dec_out = self.decoder(dec_out, enc_out, x_mask=dec_self_mask, cross_mask=dec_enc_mask)

        if self.output_attention:
            return dec_out[:, -self.pred_len:, :], attns
        else:
            return dec_out[:, -self.pred_len:, :]  # [B, L, D]

# Cell
class Informer(pl.LightningModule):
    def __init__(self, seq_len: int,
                 label_len: int, pred_len: int, output_attention: bool,
                 enc_in: int, dec_in: int, d_model: int, c_out: int,
                 embed: str, freq: str, dropout: float, factor: float,
                 n_heads: int, d_ff: int, activation: str,
                 e_layers: int, d_layers: int, distil: bool,
                 loss_train: str, loss_valid: str, loss_hypar: float,
                 learning_rate: float, lr_decay: float, weight_decay: float,
                 lr_decay_step_size: int, random_seed: int):
        super(Informer, self).__init__()
        """
        Transformer Informer model with Propspare attention.

        Parameters
        ----------
        seq_len: int
            Input sequence size.
        label_len: int
            Label sequence size.
        pred_len: int
            Prediction sequence size.
        output_attention: bool
            If true use output attention for Transformer model.
        enc_in: int
            Number of encoders in data embedding layers.
        dec_in: int
            Number of decoders in data embedding layers.
        d_model: int
            Number of nodes for embedding layers.
        c_out: int
            Number of output nodes in projection layer.
        embed: str
            Type of embedding layers.
        freq: str
            Frequency for embedding layers.
        dropout: float
            Float between (0, 1). Dropout for Transformer.
        factor: float
            Factor for attention layer.
        n_heads: int
            Number of heads in attention layer.
        d_ff: int
            Number of inputs in encoder layers.
        activation: str
            Activation function for encoder layer.
        e_layers: int
            Number of encoder layers.
        d_layers: int
            Number of decoder layers.
        distil: bool
            If true add normalization layer in encoder.
        loss_train: str
            Loss to optimize.
            An item from ['MAPE', 'MASE', 'SMAPE', 'MSE', 'MAE', 'QUANTILE', 'QUANTILE2'].
        loss_valid: str
            Validation loss.
            An item from ['MAPE', 'MASE', 'SMAPE', 'RMSE', 'MAE', 'QUANTILE'].
        loss_hypar: float
            Hyperparameter for chosen loss.
        learning_rate: float
            Learning rate between (0, 1).
        lr_decay: float
            Decreasing multiplier for the learning rate.
        weight_decay: float
            L2 penalty for optimizer.
        lr_decay_step_size: int
            Steps between each learning rate decay.
        random_seed: int
            random_seed for pseudo random pytorch initializer and
            numpy random generator.
        """

        #------------------------ Model Attributes ------------------------#
        # Architecture parameters
        self.seq_len = seq_len
        self.label_len = label_len
        self.pred_len = pred_len
        self.output_attention = output_attention
        self.enc_in = enc_in
        self.dec_in = dec_in
        self.d_model = d_model
        self.c_out = c_out
        self.embed = embed
        self.freq = freq
        self.dropout = dropout
        self.factor = factor
        self.n_heads = n_heads
        self.d_ff = d_ff
        self.activation = activation
        self.e_layers = e_layers
        self.d_layers = d_layers
        self.distil = distil

        # Loss functions
        self.loss_train = loss_train
        self.loss_hypar = loss_hypar
        self.loss_valid = loss_valid
        self.loss_fn_train = LossFunction(loss_train,
                                          seasonality=self.loss_hypar)
        self.loss_fn_valid = LossFunction(loss_valid,
                                          seasonality=self.loss_hypar)

        # Regularization and optimization parameters
        self.learning_rate = learning_rate
        self.lr_decay = lr_decay
        self.weight_decay = weight_decay
        self.lr_decay_step_size = lr_decay_step_size
        self.random_seed = random_seed

        self.model = _Informer(pred_len, output_attention,
                               enc_in, dec_in, d_model, c_out,
                               embed, freq, dropout,
                               factor, n_heads, d_ff,
                               activation, e_layers,
                               d_layers, distil)

    def forward(self, batch):
        """
        Autoformer needs batch of shape (batch_size, time, series) for y
        and (batch_size, time, exogenous) for x
        and doesnt need X for each time series.
        USE DataLoader from pytorch instead of TimeSeriesLoader.
        """

        # Protection for missing batch_size dimension
        if batch['Y'].dim()<3:
            batch['Y'] = batch['Y'][None,:,:]

        if batch['X'] is not None:
            if batch['X'].dim()<4:
                batch['X'] = batch['X'][None,:,:,:]

        if batch['sample_mask'].dim()<3:
            batch['sample_mask'] = batch['sample_mask'][None,:,:]

        Y = batch['Y'].permute(0, 2, 1)
        X = batch['X'][:, 0, :, :].permute(0, 2, 1)
        sample_mask = batch['sample_mask'].permute(0, 2, 1)
        available_mask = batch['available_mask']

        s_begin = 0
        s_end = s_begin + self.seq_len
        r_begin = s_end - self.label_len
        r_end = r_begin + self.label_len + self.pred_len

        batch_x = Y[:, s_begin:s_end, :]
        batch_y = Y[:, r_begin:r_end, :]
        batch_x_mark = X[:, s_begin:s_end, :]
        batch_y_mark = X[:, r_begin:r_end, :]
        outsample_mask = sample_mask[:, r_begin:r_end, :]

        dec_inp = torch.zeros_like(batch_y[:, -self.pred_len:, :])
        dec_inp = torch.cat([batch_y[:, :self.label_len, :], dec_inp], dim=1)

        if self.output_attention:
            forecast = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)[0]
        else:
            forecast = self.model(batch_x, batch_x_mark, dec_inp, batch_y_mark)

        batch_y = batch_y[:, -self.pred_len:, :]
        outsample_mask = outsample_mask[:, -self.pred_len:, :]

        return batch_y, forecast, outsample_mask, Y

    def training_step(self, batch, batch_idx):

        # Protection for missing batch_size dimension
        if batch['Y'].dim()<3:
            batch['Y'] = batch['Y'][None,:,:]

        outsample_y, forecast, outsample_mask, Y = self(batch)

        loss = self.loss_fn_train(y=outsample_y,
                                  y_hat=forecast,
                                  mask=outsample_mask,
                                  y_insample=Y)

        self.log('train_loss', loss, prog_bar=True, on_epoch=True)

        return loss

    def validation_step(self, batch, idx):

        # Protection for missing batch_size dimension
        if batch['Y'].dim()<3:
            batch['Y'] = batch['Y'][None,:,:]

        outsample_y, forecast, outsample_mask, Y = self(batch)

        loss = self.loss_fn_valid(y=outsample_y,
                                  y_hat=forecast,
                                  mask=outsample_mask,
                                  y_insample=Y)

        self.log('val_loss', loss, prog_bar=True)

        return loss

    def on_fit_start(self):
        torch.manual_seed(self.random_seed)
        np.random.seed(self.random_seed)
        random.seed(self.random_seed)

    def configure_optimizers(self):
        optimizer = optim.Adam(self.model.parameters(),
                               lr=self.learning_rate,
                               weight_decay=self.weight_decay)

        lr_scheduler = optim.lr_scheduler.StepLR(optimizer,
                                                 step_size=self.lr_decay_step_size,
                                                 gamma=self.lr_decay)

        return {'optimizer': optimizer, 'lr_scheduler': lr_scheduler}

# Cell
@patch
def forecast(self: Informer, Y_df: pd.DataFrame, X_df: pd.DataFrame = None,
                S_df: pd.DataFrame = None, trainer: pl.Trainer =None) -> pd.DataFrame:
    """
    Method for forecasting self.n_time_out periods after last timestamp of Y_df.

    Parameters
    ----------
    Y_df: pd.DataFrame
        Dataframe with target time-series data, needs 'unique_id','ds' and 'y' columns.
    X_df: pd.DataFrame
        Dataframe with exogenous time-series data, needs 'unique_id' and 'ds' columns.
        Note that 'unique_id' and 'ds' must match Y_df plus the forecasting horizon.
    S_df: pd.DataFrame
        Dataframe with static data, needs 'unique_id' column.
    bath_size: int
        Batch size for forecasting.
    trainer: pl.Trainer
        Trainer object for model training and evaluation.

    Returns
    ----------
    forecast_df: pd.DataFrame
        Dataframe with forecasts.
    """

    # Add forecast dates to Y_df
    Y_df['ds'] = pd.to_datetime(Y_df['ds'])
    if X_df is not None:
        X_df['ds'] = pd.to_datetime(X_df['ds'])
    self.frequency = pd.infer_freq(Y_df[Y_df['unique_id']==Y_df['unique_id'][0]]['ds']) # Infer with first unique_id series

    forecast_dates = pd.date_range(Y_df['ds'].max(), periods=self.pred_len+1, freq=self.frequency)[1:]
    index = pd.MultiIndex.from_product([Y_df['unique_id'].unique(), forecast_dates], names=['unique_id', 'ds'])
    forecast_df = pd.DataFrame({'y':[0]}, index=index).reset_index()

    Y_df = Y_df.append(forecast_df).sort_values(['unique_id','ds']).reset_index(drop=True)

    # Dataset, loader and trainer
    dataset = IterateWindowsDataset(S_df=S_df, Y_df=Y_df, X_df=X_df,
                                    mask_df=None, f_cols=[],
                                    input_size=self.seq_len,
                                    output_size=self.pred_len,
                                    ds_in_test=self.pred_len,
                                    is_test=True,
                                    verbose=True)

    loader = TimeSeriesLoader(dataset=dataset,
                                batch_size=1,
                                shuffle=False)

    if trainer is None:
        gpus = -1 if torch.cuda.is_available() else 0
        trainer = pl.Trainer(progress_bar_refresh_rate=1,
                             gpus=gpus,
                             logger=False)

    # Forecast
    outputs = trainer.predict(self, loader)

    # Process forecast and include in forecast_df
    _, forecast, _, _ = [torch.cat(output).cpu().numpy() for output in zip(*outputs)]
    forecast = np.transpose(forecast, (0, 2, 1))
    forecast_df['y'] = forecast.flatten()

    return forecast_df
