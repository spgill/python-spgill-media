#! /bin/bash

docker run --rm -v "$PWD:/src" -e "TWINE_REPOSITORY=spgill" docker.home.spgill.me/python-builder:latest
