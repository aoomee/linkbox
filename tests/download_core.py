import importlib.util
from pathlib import Path

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('linkbox',root/'linkbox.py')
lb=importlib.util.module_from_spec(spec);spec.loader.exec_module(lb)
release={}
for line in Path('/etc/os-release').read_text().splitlines():
    if '=' in line:
        k,v=line.split('=',1);release[k]=v.strip('"')
target=root/'.test-core';target.mkdir(exist_ok=True)
print(lb.fetch_core(target,release['ID']))
