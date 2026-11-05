import os
import sys
import tempfile
from pathlib import Path

def setup(root):
    root=Path(root).resolve()
    runtime=root/".runtime"
    temp=runtime/"tmp"
    temp.mkdir(parents=True,exist_ok=True)
    os.environ["TEMP"]=os.environ["TMP"]=str(temp)
    tempfile.tempdir=str(temp)
    os.environ["CRAWL4_AI_BASE_DIRECTORY"]=str(runtime)
    os.environ["CRAWL4AI_NO_CLOUD_NOTICE"]="1"
    os.environ["PYTHONDONTWRITEBYTECODE"]="1"
    sys.dont_write_bytecode=True
    vendor=root/"vendor"
    if str(vendor) not in sys.path:sys.path.insert(0,str(vendor))
