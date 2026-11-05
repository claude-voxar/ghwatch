import argparse


def build_parser():
    return argparse.ArgumentParser(prog="ghwatch")


def main(argv=None):
    build_parser().parse_args(argv)
