#!/usr/bin/env python3

import logging
import os
import re
import runpy
import sys
from contextlib import contextmanager
from pathlib import Path

GRPC_TOOLS_PROTOC_MODULE_NAME = 'grpc_tools.protoc'
PROJECT_ROOT_DIR = Path(__file__).parents[1]
PROTO_DIR = Path('proto')
OUT_DIR = Path('pb')
IMPORT_LINE_PATTERN = re.compile(r'^import ([a-zA-Z0-9_]+)_pb2 as ([a-zA-Z0-9_]+)__pb2$', re.MULTILINE)
IMPORT_LINE_REPLACEMENT = r'from . import \1_pb2 as \2__pb2'
FROM_IMPORT_LINE_PATTERN = re.compile(r'^from ([a-zA-Z0-9_]+)_pb2 import', re.MULTILINE)
FROM_IMPORT_LINE_REPLACEMENT = r'from .\1_pb2 import'

logger = logging.getLogger(Path(__file__).stem)


@contextmanager
def change_working_directory(target_cwd: Path):
    original_cwd = Path.cwd()
    os.chdir(target_cwd)
    try:
        yield
    finally:
        os.chdir(original_cwd)


def fix_imports(out_dir: Path):
    for py_file in out_dir.glob('*.py'):
        content = py_file.read_text()
        for pattern, replacement in zip(
            (IMPORT_LINE_PATTERN, FROM_IMPORT_LINE_PATTERN),
            (IMPORT_LINE_REPLACEMENT, FROM_IMPORT_LINE_REPLACEMENT),
            strict=True,
        ):
            content = pattern.sub(replacement, content)
        py_file.write_text(content)


def generate_protos():
    output_directory = PROJECT_ROOT_DIR / OUT_DIR
    output_directory.mkdir(parents=True, exist_ok=True)

    with change_working_directory(PROJECT_ROOT_DIR):
        protoc_args = [
            f'--proto_path={PROTO_DIR}',
            f'--python_out={OUT_DIR}',
            f'--grpc_python_out={OUT_DIR}',
            str(PROTO_DIR / '*.proto'),
        ]
        logger.info(
            'Running "%s" module with args: "%s"',
            GRPC_TOOLS_PROTOC_MODULE_NAME,
            protoc_args,
        )
        try:
            sys.argv[1:] = protoc_args
            runpy.run_module(GRPC_TOOLS_PROTOC_MODULE_NAME, run_name='__main__', alter_sys=True)
        except SystemExit as e:
            if e.code != 0:
                raise RuntimeError(
                    f'Failed to compile .proto files with protoc - '
                    f'cwd: "{PROJECT_ROOT_DIR}", args: "{protoc_args}, exit code {e.code}.'
                ) from e
        logger.info('Proto files has been compiled successfully to "%s".', output_directory)

    logger.info('Fixing imports to relative for *_pb2.py files in "%s"...', output_directory)
    fix_imports(output_directory)
    logger.info('Imports has been fixes successfully for *_pb2.py files in "%s".', output_directory)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    generate_protos()
