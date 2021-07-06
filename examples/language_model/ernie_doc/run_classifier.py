# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import collections

import os
import random
from functools import partial
import time

import numpy as np
import paddle
import paddle.nn as nn
from paddle.io import DataLoader
from paddlenlp.transformers import ErnieDocModel, ErnieDocForSequenceClassification, BPETokenizer
from paddlenlp.transformers import LinearDecayWithWarmup
from paddlenlp.utils.log import logger
from paddlenlp.datasets import load_dataset
from paddlenlp.data import Stack

from data import ClassifierIterator

# yapf: disable
parser = argparse.ArgumentParser()
parser.add_argument("--batch_size", default=16, type=int, help="Batch size per GPU/CPU for training.")
parser.add_argument("--model_name_or_path", type=str, default="ernie-doc-base-en", help="pretraining model name or path")
parser.add_argument("--max_seq_length", type=int, default=512, help="The maximum total input sequence length after SentencePiece tokenization.")
parser.add_argument("--learning_rate", type=float, default=7e-5, help="Learning rate used to train.")
parser.add_argument("--max_steps", default=10000, type=int, help="Max training steps to train.")
parser.add_argument("--save_steps", type=int, default=1000, help="Save checkpoint every X updates steps.")
parser.add_argument("--logging_steps", type=int, default=1, help="Log every X updates steps.")
parser.add_argument("--output_dir", type=str, default='checkpoints/', help="Directory to save model checkpoint")
parser.add_argument("--epochs", type=int, default=3, help="Number of epoches for training.")
parser.add_argument("--attn_dropout", type=float, default=0.0, help="Attention ffn model dropout.")
parser.add_argument("--hidden_dropout_prob", type=float, default=0.0, help="The dropout rate for the embedding pooler.")
parser.add_argument("--device", type=str, default="gpu", choices=["cpu", "gpu"], help="Select cpu, gpu devices to train model.")
parser.add_argument("--seed", type=int, default=1, help="Random seed for initialization.")
parser.add_argument("--memory_length", type=int, default=128, help="Random seed for initialization.")
parser.add_argument("--weight_decay", default=0.01, type=float, help="Weight decay if we apply some.")
parser.add_argument("--warmup_proportion", default=0.1, type=float, help="Linear warmup proption over the training process.")
parser.add_argument("--num_labels", default=2, type=int, help="The number of labels.")

# yapf: enable
args = parser.parse_args()


def set_seed(args):
    # Use the same data seed(for data shuffle) for all procs to guarantee data
    # consistency after sharding.
    random.seed(args.seed)
    np.random.seed(args.seed)
    # Maybe different op seeds(for dropout) for different procs is better. By:
    # `paddle.seed(args.seed + paddle.distributed.get_rank())`
    paddle.seed(args.seed)


def init_memory(batch_size, memory_length, d_model, n_layers):
    return [
        paddle.zeros(
            [batch_size, memory_length, d_model], dtype="float32")
        for _ in range(n_layers)
    ]


@paddle.no_grad()
def evaluate(model, criterion, metric, data_loader, memories0):
    """
    Given a dataset, it evals model and computes the metric.

    Args:
        model(obj:`paddle.nn.Layer`): A model to classify texts.
        data_loader(obj:`paddle.io.DataLoader`): The dataset loader which generates batches.
        criterion(obj:`paddle.nn.Layer`): It can compute the loss.
        metric(obj:`paddle.metric.Metric`): The evaluation metric.
    """
    model.eval()
    losses = []
    # copy the memory
    memories = list(memories0)
    tic_train = time.time()
    eval_logging_step = 500
    for step, batch in enumerate(data_loader, start=1):
        input_ids, position_ids, token_type_ids, attn_mask, labels, gather_idxs, need_cal_loss = batch
        logits, memories = model(input_ids, memories, token_type_ids,
                                 position_ids, attn_mask)
        logits, labels = list(
            map(lambda x: paddle.gather(x, gather_idxs), [logits, labels]))
        loss = criterion(logits, labels) * need_cal_loss
        losses.append(loss.mean().numpy())
        correct = metric.compute(logits, labels) * need_cal_loss
        metric.update(correct)
        if step % eval_logging_step == 0:
            logger.info("Step %d: loss:  %.5f, accu: %.5f, speed: %.5f steps/s"
                        % (step, np.mean(losses), metric.accumulate(),
                           eval_logging_step / (time.time() - tic_train)))
            tic_train = time.time()
    acc = metric.accumulate()
    logger.info("Eval loss: %.5f, accu: %.5f" % (np.mean(losses), acc))
    model.train()
    metric.reset()
    return acc


def do_train(args):
    paddle.set_device(args.device)
    trainer_num = paddle.distributed.get_world_size()
    if trainer_num > 1:
        paddle.distributed.init_parallel_env()
    rank = paddle.distributed.get_rank()
    set_seed(args)
    if rank == 0:
        if os.path.exists(args.model_name_or_path):
            logger.info("init checkpoint from %s" % args.model_name_or_path)
    model = ErnieDocForSequenceClassification.from_pretrained(
        args.model_name_or_path, num_classes=2)

    model_config = model.ernie_doc.config
    if trainer_num > 1:
        model = paddle.DataParallel(model)
    tokenizer = BPETokenizer.from_pretrained(args.model_name_or_path)

    train_ds, test_ds = load_dataset("imdb", splits=["train", "test"])

    train_ds_iter = ClassifierIterator(
        train_ds,
        args.batch_size,
        tokenizer,
        trainer_num,
        trainer_id=rank,
        memory_len=model_config["memory_len"],
        max_seq_length=args.max_seq_length,
        random_seed=args.seed)
    test_ds_iter = ClassifierIterator(
        test_ds,
        args.batch_size,
        tokenizer,
        trainer_num,
        trainer_id=rank,
        memory_len=model_config["memory_len"],
        max_seq_length=args.max_seq_length,
        mode="test")

    train_dataloader = paddle.io.DataLoader.from_generator(
        capacity=70, return_list=True)
    train_dataloader.set_batch_generator(train_ds_iter, paddle.get_device())
    test_dataloader = paddle.io.DataLoader.from_generator(
        capacity=70, return_list=True)
    test_dataloader.set_batch_generator(test_ds_iter, paddle.get_device())

    num_training_examples = train_ds_iter.get_num_examples()
    num_training_steps = args.epochs * num_training_examples // args.batch_size // trainer_num
    logger.info("Device count: %d, trainer_id: %d" % (trainer_num, rank))
    logger.info("Num train examples: %d" % num_training_examples)
    logger.info("Max train steps: %d" % num_training_steps)
    logger.info("Num warmup steps: %d" % int(num_training_steps *
                                             args.warmup_proportion))

    lr_scheduler = LinearDecayWithWarmup(args.learning_rate, num_training_steps,
                                         args.warmup_proportion)

    # Generate parameter names needed to perform weight decay.
    # All bias and LayerNorm parameters are excluded.
    decay_params = [
        p.name for n, p in model.named_parameters()
        if not any(nd in n for nd in ["bias", "norm"])
    ]
    optimizer = paddle.optimizer.AdamW(
        learning_rate=lr_scheduler,
        parameters=model.parameters(),
        weight_decay=args.weight_decay,
        apply_decay_param_fun=lambda x: x in decay_params)

    criterion = paddle.nn.loss.CrossEntropyLoss()
    metric = paddle.metric.Accuracy()
    eval_metric = paddle.metric.Accuracy()

    global_steps = 0
    best_acc = -1
    create_memory = partial(init_memory, args.batch_size, args.memory_length,
                            model_config["hidden_size"],
                            model_config["num_hidden_layers"])
    # copy the memory
    memories = create_memory()
    tic_train = time.time()
    for epoch in range(args.epochs):
        train_ds_iter.shuffle_sample()
        train_dataloader.set_batch_generator(train_ds_iter, paddle.get_device())
        for step, batch in enumerate(train_dataloader, start=1):
            global_steps += 1
            input_ids, position_ids, token_type_ids, attn_mask, labels, gather_idx, need_cal_loss = batch
            logits, memories = model(input_ids, memories, token_type_ids,
                                     position_ids, attn_mask)

            logits, labels = list(
                map(lambda x: paddle.gather(x, gather_idx), [logits, labels]))
            loss = criterion(logits, labels) * need_cal_loss
            mean_loss = loss.mean()
            mean_loss.backward()
            optimizer.step()
            lr_scheduler.step()
            optimizer.clear_grad()

            acc = metric.compute(logits, labels) * need_cal_loss
            metric.update(acc)

            if global_steps % args.logging_steps == 0:
                logger.info(
                    "train: global step %d, epoch: %d, loss: %f, acc:%f, lr: %f, speed: %.2f step/s"
                    % (global_steps, epoch, mean_loss, metric.accumulate(),
                       lr_scheduler.get_lr(),
                       args.logging_steps / (time.time() - tic_train)))
                tic_train = time.time()

            if global_steps % args.save_steps == 0 or global_steps == num_training_steps:
                # evaluate
                logger.info("Eval:")
                eval_acc = evaluate(model, criterion, eval_metric,
                                    test_dataloader, create_memory())
                # save
                if rank == 0:
                    output_dir = os.path.join(args.output_dir,
                                              "model_%d" % (global_steps))
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = model._layers if isinstance(
                        model, paddle.DataParallel) else model
                    model_to_save.save_pretrained(output_dir)
                    tokenizer.save_pretrained(output_dir)
                    if eval_acc > best_acc:
                        best_acc = eval_acc
                        best_model_dir = os.path.join(args.output_dir,
                                                      "best_model")
                        if not os.path.exists(best_model_dir):
                            os.makedirs(best_model_dir)
                        model_to_save.save_pretrained(best_model_dir)
                        tokenizer.save_pretrained(output_dir)


if __name__ == "__main__":
    do_train(args)