import os
import sys
import numpy as np
import argparse


def script_options():
    parser = argparse.ArgumentParser()
    required_params = parser.add_argument_group("Required arguments")
    required_params.add_argument("-W", action="store", dest="width",
                        help="Width of raw data")
    required_params.add_argument("-H", action="store", dest="height",
                        help="Height of raw data")
    required_params.add_argument("-C", action="store", dest="channel",
                        help="Channel of raw data")
    required_params.add_argument("-O", action="store", dest="output_path", type=str,
                        help="Output Path")
    return parser

def gen_random(height, width, channel, out):
    data = np.random.rand(height, width, channel)
    data[data < 0] = 0
    data[data > 1] = 1
    data = data.astype(np.float32)
    filename = os.path.join(out, 'random'+str(height)+'_' + str(width) + '_' +str(channel)+'.raw')
    if os.path.exists(filename):
        os.remove(filename)

    data.tofile(filename)


if __name__ == '__main__':

    parser = script_options()
    if len(sys.argv) < 4:
        parser.print_help()
        sys.exit(0)

    options = parser.parse_args()
    gen_random(int(options.height), int(options.width), int(options.channel), options.output_path)
