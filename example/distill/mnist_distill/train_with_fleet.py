#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
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

from __future__ import print_function
import os

if os.environ.get('PADDLE_TRAINER_ENDPOINTS') is None:
    os.environ['PADDLE_TRAINER_ENDPOINTS'] = '127.0.0.1:0'

from paddle_edl.distill.distill_reader import DistillReader
import argparse
import ast
from PIL import Image
import numpy
import paddle
import paddle.fluid as fluid
from paddle.fluid.incubate.fleet.collective import fleet
from paddle.fluid.incubate.fleet.base import role_maker


def parse_args():
    parser = argparse.ArgumentParser("mnist")
    parser.add_argument(
        '--use_gpu',
        type=ast.literal_eval,
        default=True,
        help="Whether to use GPU or not. 'True' or 'False'")
    parser.add_argument(
        '--num_epochs', type=int, default=5, help="number of epochs.")
    parser.add_argument(
        '--use_distill_service',
        default=False,
        type=ast.literal_eval,
        help="Whether to use distill service train. 'True' or 'False'")
    parser.add_argument(
        '--save_serving_model',
        default=False,
        type=ast.literal_eval,
        help="Whether to save paddle serving model. 'True' or 'False'")
    parser.add_argument(
        '--distill_teachers',
        default='127.0.0.1:9292',
        type=str,
        help="teachers of distill train. such as '127.0.0.1:9292,127.0.0.1:9293'"
    )
    args = parser.parse_args()
    return args


def loss_net(hidden, label):
    prediction = fluid.layers.fc(input=hidden, size=10, act='softmax')
    loss = fluid.layers.cross_entropy(input=prediction, label=label)
    avg_loss = fluid.layers.mean(loss)
    acc = fluid.layers.accuracy(input=prediction, label=label)
    return prediction, avg_loss, acc


def multilayer_perceptron(img, label):
    img = fluid.layers.fc(input=img, size=200, act='tanh')
    hidden = fluid.layers.fc(input=img, size=200, act='tanh')
    return loss_net(hidden, label)


def softmax_regression(img, label):
    return loss_net(img, label)


def convolutional_neural_network(img, label):
    conv_pool_1 = fluid.nets.simple_img_conv_pool(
        input=img,
        filter_size=5,
        num_filters=20,
        pool_size=2,
        pool_stride=2,
        act="relu")
    conv_pool_1 = fluid.layers.batch_norm(conv_pool_1)
    conv_pool_2 = fluid.nets.simple_img_conv_pool(
        input=conv_pool_1,
        filter_size=5,
        num_filters=50,
        pool_size=2,
        pool_stride=2,
        act="relu")
    return loss_net(conv_pool_2, label)


def train(nn_type,
          use_cuda,
          save_dirname=None,
          model_filename=None,
          params_filename=None):
    if use_cuda and not fluid.core.is_compiled_with_cuda():
        return

    startup_program = fluid.default_startup_program()
    main_program = fluid.default_main_program()

    train_reader = paddle.batch(
        paddle.reader.shuffle(
            paddle.dataset.mnist.train(), buf_size=500),
        batch_size=BATCH_SIZE)

    test_reader = paddle.batch(
        paddle.dataset.mnist.test(), batch_size=BATCH_SIZE)

    img = fluid.data(name='img', shape=[None, 1, 28, 28], dtype='float32')
    label = fluid.data(name='label', shape=[None, 1], dtype='int64')

    if nn_type == 'softmax_regression':
        net_conf = softmax_regression
    elif nn_type == 'multilayer_perceptron':
        net_conf = multilayer_perceptron
    else:
        net_conf = convolutional_neural_network

    prediction, avg_loss, acc = net_conf(img, label)

    test_program = main_program.clone(for_test=True)

    inputs = [img, label]
    test_inputs = [img, label]

    if args.use_distill_service:
        dr = DistillReader(ins=['img', 'label'], predicts=['fc_0.tmp_2'])
        dr.set_fixed_teacher(args.distill_teachers)
        train_reader = dr.set_sample_list_generator(train_reader)

        soft_label = fluid.data(
            name='soft_label', shape=[None, 10], dtype='float32')
        inputs.append(soft_label)
        distill_loss = fluid.layers.cross_entropy(
            input=prediction, label=soft_label, soft_label=True)
        #distill_loss = fluid.layers.mse_loss(input=prediction, label=soft_label)
        distill_loss = fluid.layers.mean(distill_loss)
        #loss = 0.3 * avg_loss + distill_loss
        loss = distill_loss
    else:
        loss = avg_loss

    role = role_maker.PaddleCloudRoleMaker(is_collective=True)
    fleet.init(role)
    train_rank = fleet.worker_index()
    train_nranks = fleet.worker_num()

    optimizer = fluid.optimizer.Momentum(learning_rate=0.001, momentum=0.9)
    if use_cuda:
        optimizer = fleet.distributed_optimizer(optimizer)
    optimizer.minimize(loss)

    main_program = fleet.main_program if use_cuda else main_program
    gpu_id = int(os.getenv("FLAGS_selected_gpus", "0"))

    def train_test(train_test_program, train_test_reader):
        acc_set = []
        avg_loss_set = []
        for test_data in train_test_reader():
            acc_np, avg_loss_np = exe.run(program=train_test_program,
                                          feed=test_data,
                                          fetch_list=[acc, avg_loss])
            acc_set.append(float(acc_np))
            avg_loss_set.append(float(avg_loss_np))
        # get test acc and loss
        acc_val_mean = numpy.array(acc_set).mean()
        avg_loss_val_mean = numpy.array(avg_loss_set).mean()
        return avg_loss_val_mean, acc_val_mean

    place = fluid.CUDAPlace(gpu_id) if use_cuda else fluid.CPUPlace()
    reader_places = fluid.cuda_places() if use_cuda else fluid.CPUPlace()

    py_train_reader = fluid.io.DataLoader.from_generator(
        feed_list=inputs, capacity=64)
    py_train_reader.set_sample_list_generator(train_reader, reader_places)
    py_test_reader = fluid.io.DataLoader.from_generator(
        feed_list=test_inputs, capacity=64)
    py_test_reader.set_sample_list_generator(test_reader, place)

    exe = fluid.Executor(place)
    exe.run(startup_program)
    epochs = [epoch_id for epoch_id in range(NUM_EPOCHS)]

    lists = []
    step = 0
    for epoch_id in epochs:
        for step_id, data in enumerate(py_train_reader()):
            metrics = exe.run(main_program, feed=data, fetch_list=[loss, acc])
            if step % 100 == 0:
                print("Pass {}, Step {}, Cost {}".format(epoch_id, step,
                                                         metrics[0].mean()))
            step += 1

        if train_rank == 0:
            # test for epoch
            avg_loss_val, acc_val = train_test(
                train_test_program=test_program,
                train_test_reader=py_test_reader)

            print("Test with Pass %d, avg_cost: %s, acc: %s" %
                  (epoch_id, avg_loss_val, acc_val))
            lists.append((epoch_id, avg_loss_val, acc_val))
            if save_dirname is not None:
                fluid.io.save_inference_model(
                    save_dirname, ["img"], [prediction],
                    exe,
                    model_filename=model_filename,
                    params_filename=params_filename)

    if train_rank == 0:
        if args.save_serving_model:
            import paddle_serving_client.io as serving_io
            if not os.path.isdir('output'):
                os.mkdir('output')
            serving_io.save_model("output/mnist_cnn_model",
                                  "output/serving_conf", {img.name: img},
                                  {prediction.name: prediction}, test_program)
            print('save serving model, feed_names={}, fetch_names={}'.format(
                [img.name], [prediction.name]))

        # find the best pass
        best = sorted(lists, key=lambda list: float(list[1]))[0]
        print('Best pass is %s, testing Avg cost is %s' % (best[0], best[1]))
        print('The classification accuracy is %.2f%%' % (float(best[2]) * 100))


def infer(use_cuda,
          save_dirname=None,
          model_filename=None,
          params_filename=None):
    if save_dirname is None:
        return

    gpu_id = int(os.getenv("FLAGS_selected_gpus", "0"))
    place = fluid.CUDAPlace(gpu_id) if use_cuda else fluid.CPUPlace()
    exe = fluid.Executor(place)

    def load_image(file):
        im = Image.open(file).convert('L')
        im = im.resize((28, 28), Image.ANTIALIAS)
        im = numpy.array(im).reshape((1, 1, 28, 28)).astype(numpy.float32)
        im = im / 255.0 * 2.0 - 1.0
        return im

    cur_dir = os.path.dirname(os.path.realpath(__file__))
    tensor_img = load_image(cur_dir + '/image/infer_3.png')

    inference_scope = fluid.core.Scope()
    with fluid.scope_guard(inference_scope):
        # Use fluid.io.load_inference_model to obtain the inference program desc,
        # the feed_target_names (the names of variables that will be feeded
        # data using feed operators), and the fetch_targets (variables that
        # we want to obtain data from using fetch operators).
        [inference_program, feed_target_names,
         fetch_targets] = fluid.io.load_inference_model(
             save_dirname, exe, model_filename, params_filename)

        # Construct feed as a dictionary of {feed_target_name: feed_target_data}
        # and results will contain a list of data corresponding to fetch_targets.
        results = exe.run(inference_program,
                          feed={feed_target_names[0]: tensor_img},
                          fetch_list=fetch_targets)
        lab = numpy.argsort(results)
        print("Inference result of image/infer_3.png is: %d" % lab[0][0][-1])


def main(use_cuda, nn_type):
    model_filename = None
    params_filename = None
    save_dirname = "recognize_digits_" + nn_type + ".inference.model"

    # call train() with is_local argument to run distributed train
    train(
        nn_type=nn_type,
        use_cuda=use_cuda,
        save_dirname=save_dirname,
        model_filename=model_filename,
        params_filename=params_filename)
    infer(
        use_cuda=use_cuda,
        save_dirname=save_dirname,
        model_filename=model_filename,
        params_filename=params_filename)


if __name__ == '__main__':
    args = parse_args()
    BATCH_SIZE = 64
    NUM_EPOCHS = args.num_epochs
    use_cuda = args.use_gpu
    # predict = 'softmax_regression' # uncomment for Softmax
    #predict = 'multilayer_perceptron' # uncomment for MLP
    predict = 'convolutional_neural_network'  # uncomment for LeNet5
    main(use_cuda=use_cuda, nn_type=predict)
