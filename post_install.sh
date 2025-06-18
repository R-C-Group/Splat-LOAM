#!/bin/bash

# 确保在conda环境中
source activate Splat-LOAM

# 设置构建环境变量
export CUDA_HOME=$CONDA_PREFIX
export CUDACXX=$CUDA_HOME/bin/nvcc
export PATH=$CUDA_HOME/bin:$PATH
export LD_LIBRARY_PATH=$CUDA_HOME/lib64:$LD_LIBRARY_PATH
export MAX_JOBS=4

# 安装本地模块
pip install --no-build-isolation -v -e ./submodules/diff-surfel-spherical-rasterization/
pip install --no-build-isolation -v -e ./submodules/gsaligner/
pip install --no-build-isolation -v -e ./submodules/simple-knn/