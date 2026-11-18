# 项目本地 PyArrow

Parquet 接口使用 `pyarrow==26.0.1`，安装于本项目 `vendor/`，未修改全局 Conda 环境。二进制目录不纳入 Git；版本固定在根目录 `requirements.txt`。

在当前 Windows/Python 3.11 环境重建：

```powershell
& C:/Users/marti/anaconda3/envs/finance_crawler/python.exe -m pip install --target D:/financial_data_project2/vendor pyarrow==26.0.1 --no-deps --no-cache-dir --index-url https://pypi.org/simple
```

安装包保留自身 `pyarrow-26.0.1.dist-info/licenses/` 中的许可证与 NOTICE。接口导出延迟加载依赖，缺失时记录 `dependency_missing`，正式 JSON 不受影响；补装后 `main.py --export-parquet` 可重建。

参考：[Apache Arrow 安装文档](https://arrow.apache.org/docs/python/install.html)、[PyArrow 发行页面](https://pypi.org/project/pyarrow/)。
