#!/bin/bash
pip install -e .
julia --project=. -e "using Pkg; Pkg.instantiate()"