import argparse
import time
import pdb
import numpy as np
from indexed import IndexedOrderedDict

from load_binary_score import BinaryDataSet, BinaryARMLCDataSet
from binary_model import BinaryClassifier
from transforms import *

from torch import multiprocessing
from torch.utils import model_zoo
from ops.utils import get_actionness_configs, get_reference_model_url

global args
parser = argparse.ArgumentParser(description = 'extract actionnes score')
parser.add_argument('dataset', type=str, choices=['activitynet1.2', 'thumos14', 'armlc'])
parser.add_argument('modality', type=str, choices=['RGB', 'Flow', 'RGBDiff'])
parser.add_argument('subset', type=str, choices=['training','validation','testing'])
parser.add_argument('weights', type=str)
parser.add_argument('save_scores', type=str)
parser.add_argument('--arch', type=str, default='BNInception')
parser.add_argument('--save_raw_scores', type=str, default=None)
parser.add_argument('--video_list_file', type=str, default=None)
parser.add_argument('--frame_interval', type=int, default=5)
parser.add_argument('--test_batchsize', type=int, default=512)
parser.add_argument('--max_num', type=int, default=-1)
parser.add_argument('--test_crops', type=int, default=10)
parser.add_argument('--input_size', type=int, default=224)
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--gpus', nargs='+', type=int, default=None)
parser.add_argument('--flow_pref', type=str, default='')
parser.add_argument('--use_reference', default=False, action='store_true')
parser.add_argument('--use_kinetics_reference', default=False, action='store_true')

args = parser.parse_args()

if args.dataset != 'armlc':
    dataset_configs = get_actionness_configs(args.dataset)
    num_class = dataset_configs['num_class']
else:
    print('start using armlc dataset, using binary actionness classifier...')
    num_class = 2

if args.dataset == 'thumos14':
    if args.subset == 'validation':
        test_prop_file = 'data/{}_proposal_list.txt'.format(dataset_configs['train_list'])
    elif args.subset == 'testing':
        test_prop_file = 'data/{}_proposal_list.txt'.format(dataset_configs['test_list'])
elif args.dataset == 'activitynet1.2':
    if args.subset == 'training':
        test_prop_file = 'data/{}_proposal_list.txt'.format(dataset_configs['train_list'])
    elif args.subset == 'validation':    
        test_prop_file = 'data/{}_proposal_list.txt'.format(dataset_configs['test_list'])
elif args.dataset == 'armlc':
    print('start testing armlc dataset, only testing phase is supported')
    assert args.subset == 'testing'

if args.modality == 'RGB':
    data_length = 1
elif args.modality in ['Flow', 'RGBDiff']:
    data_length = 5
else:
    raise ValueError('unknown modality {}'.format(args.modality))

gpu_list = args.gpus if args.gpus is not None else range(8)
print('gpu list: ', gpu_list)
assert args.save_scores


def parse_video_list(video_list_file):
        """
        This function parses the video list.
        We assume the video list is in the following format.
        So there should be not be any space in the video directory names
        =======
        num_videos
        video_0_directory video_0_num_frames (video_0_label)
        video_1_directory video_1_num_frames (video_1_label)
        video_2_directory video_2_num_frames (video_2_label)
        ...
        ...
        =======

        (labels) are optional
        """
        video_dict = IndexedOrderedDict()
        if hasattr(video_list_file, 'read'):
            lst_file = video_list_file.read().split('\n')
        elif isinstance(video_list_file, str):
            lst_file = open(video_list_file)
        else:
            raise TypeError('incorrect type of video list file!')
        items = [x.strip().split() for x in lst_file]
        print(items[0])
        num_videos = int(items[0][0])

        for it in items[1:]:
            video_key = it[0]
            video_num_frame = int(it[1])
            video_path = video_key

            # write the video record to the indexedOrderedDict
            video_dict[video_key] = (video_path, video_num_frame)

        assert num_videos == len(video_dict)
        print("all videos indexed. got {} videos".format(len(video_dict)))
        return video_dict


def runner_func(dataset, state_dict, gpu_id, index_queue, result_queue):
    torch.cuda.set_device(gpu_id)
    net = BinaryClassifier(num_class, 5,
                           args.modality, test_mode=True, new_length=data_length,
                           base_model=args.arch)

    net.load_state_dict(state_dict)
    net.prepare_test_fc()
    net.eval()
    net.cuda()
    output_dim = net.test_fc.out_features
    while True:
        index = index_queue.get()
        frames_gen, frame_cnt = dataset[index]
        num_crop = args.test_crops
        length = 3
        if args.modality == 'Flow':
            length = 10
        elif args.modality == 'RGBDiff':
            length = 18

        output = torch.zeros((frame_cnt, num_crop, output_dim)).cuda()
        cnt = 0
        for frames in frames_gen:
            input_var = torch.autograd.Variable(frames.view(-1, length, frames.size(-2), frames.size(-1)).cuda(),
                                                volatile=True)
            rst, _ = net(input_var, None)
            sc = rst.data.view(-1, num_crop, output_dim)
            output[cnt:cnt + sc.size(0), :, :] = sc
            cnt += sc.size(0)
        if hasattr(dataset, 'video_list'):
            result_queue.put((dataset.video_list[index].id.split('/')[-1], output.cpu().numpy()))
        elif hasattr(dataset, 'video_dict'): 
            result_queue.put((dataset.video_dict.keys()[index].split('/')[-1], output.cpu().numpy()))



if __name__ == '__main__':

    ctx = multiprocessing.get_context('spawn')
    net = BinaryClassifier(num_class, 5,
                           args.modality,
                           base_model=args.arch)

    if args.test_crops == 1:
        cropping = torchvision.transforms.Compose([
            GroupScale(net.scale_size),
            GroupScale(net.input_size),
        ])
    elif args.test_crops == 10:
        cropping = torchvision.transforms.Compose([
            GroupOverSample(net.input_size, net.scale_size)
        ])
    else:
        raise ValueError("only 1 and 10 crops are supported while we got {}".format(args.test_crop))

    if not args.use_reference and not args.use_kinetics_reference:
        checkpoint = torch.load(args.weights)
        print('Done loading model checkpoint')
    else:
        model_url = get_reference_model_url(args.dataset, args.modality, 
                                            'ImageNet' if args.use_reference else 'Kinetics', args.arch)
        checkpoint = model_zoo.load_url(model_url)
        print("use reference model: {}".format(model_url))

    print("model epoch {} loss: {}".format(checkpoint['epoch'], checkpoint['best_loss']))
    base_dict = {'.'.join(k.split('.')[1:]): v for k, v in list(checkpoint['state_dict'].items())}
    
    if args.dataset != 'armlc':
        dataset = BinaryDataSet("", test_prop_file,
                            new_length=data_length,
                            modality=args.modality,
                            image_tmpl="img_{:05d}.jpg" if args.modality in ["RGB",
                                                                             "RGBDiff"] else args.flow_pref + "{}_{:05d}.jpg",
                            test_mode=True, test_interval=args.frame_interval,
                            transform=torchvision.transforms.Compose([
                                cropping,
                                Stack(roll=(args.arch in ['BNInception', 'InceptionV3'])),
                                ToTorchFormatTensor(div=(args.arch not in ['BNInception', 'InceptionV3'])),
                                GroupNormalize(net.input_mean, net.input_std),
                            ]), verbose=False)
    else:
        print('using armlc dataset!!!')
        assert args.video_list_file
        video_dict = parse_video_list(args.video_list_file)
        dataset = BinaryARMLCDataSet(video_dict, data_length, 
                            args.frame_interval,
                            modality=args.modality,
                            image_tmpl="img_{:05d}.jpg" if args.modality in ["RGB",
                                                                            "RGBDiff"] else args.flow_pref + "_{}_{:05d}.jpg",
                            transform=torchvision.transforms.Compose([
                                cropping,
                                Stack(roll=(args.arch in ['BNInception', 'InceptionV3'])),
                                ToTorchFormatTensor(div=(args.arch not in ['BNInception', 'InceptionV3'])),
                                GroupNormalize(net.input_mean, net.input_std),
                            ]))

    index_queue = ctx.Queue()
    result_queue = ctx.Queue()
    workers = [ctx.Process(target=runner_func, args=(dataset,base_dict, gpu_list[i % len(gpu_list)], index_queue, result_queue))
               for i in range(args.workers)]

    del net

    max_num = args.max_num if args.max_num > 0 else len(dataset)


    for i in range(max_num):
        index_queue.put(i)


    for w in workers:
        w.daemon = True
        w.start()


    proc_start_time = time.time()
    out_dict = {}
    for i in range(max_num):
        rst = result_queue.get()
        out_dict[rst[0]] = rst[1] 
        cnt_time = time.time() - proc_start_time
        print('video {} done, shape: {}, total {}/{}, average {:.04f} sec/video'.format(i, rst[1].shape, i + 1,
                                                                        max_num,
                                                                        float(cnt_time) / (i+1)))

    if args.save_scores is not None:
        save_dict = {k: v for k,v in out_dict.items()}
        import pickle

        pickle.dump(save_dict, open(args.save_scores, 'wb'), 2)

