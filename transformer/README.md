### Pure Vanilla Transformer from scratch

I started this project because I wanted a clearer understanding of the Transformer architecture. While reading *Attention Is All You Need*, I focused on implementing it as closely as possible to the original paper. However, most available implementations included later more advanced and improvements. That made me wonder if I fully understood the original design.

So I built this version from scratch. It follows the paper closely by default, but also allows you to enable more modern practices to see how they affect performance and compare results.

### Quickstart

* Clone the project and install requirements:
```bash
git clone git@github.com:Tialo/nlp-milestones-from-scratch.git
cd nlp-milestones-from-scratch/transformer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
* To **download data** for training, simply run:
```bash
python data_utils.py --save_path data --train_fraction 0.8
```
* To **train** a model on downloaded data, run:
```bash
python train.py --train_path data/train.json --val_path data/val.json --save_path model
```
* To **evaluate** a model and compute the BLEU score, run:
```bash
python evaluate_model.py --model_path model --tokenizer_path tokenizer.json --val_path data/val.json
```
* To change architecture or training parameters, see:
```bash
python train.py --help
```

### Training Tweaks Explained

This project is heavily inspired by [Annotated Transformer](https://github.com/harvardnlp/annotated-transformer/), where most advanced techniques were used.

* `--tie_embeddings` and `--no_tie_embeddings`

    Some implementations neglected tying embeddings with the pre-softmax layer. This not only reduces the number of model parameters (by 4 millions for a vocab_size of 8,192), but in my case, it also sped up convergence and improved BLEU results by ~10%. These parameters control embedding tying.

* `--post_ln` and `--pre_ln`

    These parameters control the order in which Layer Normalization is applied in Encoder and Decoder Layers. [See also](https://github.com/harvardnlp/annotated-transformer/issues/92#issuecomment-1132966376) for more historical context. If `pre_ln` is chosen, applies Layer Normalization to Encoder and Decoder (not individual layers) outputs, as it is done in GPT2 paper.

* `--use_additional_dropout`

    It's common to use dropout after softmax in scaled dot-product attention and in the Feed Forward layer inside Multi-Head Attention layer, but neither of these were proposed in the original paper. This parameter sets dropout rates in these parts to 0.1.

* `--xavier_initialization`

    Initialize model parameters using `xavier_uniform_`.

### Note on the training

If you will use more/less data or epochs, tune `warmup_fraction` accordingly
