from pathlib import Path
from sys import platform
import subprocess
import os

def getFileURI(name: str)->str:
    cwd = Path.cwd()
    return str(cwd) + "/" + name

def getEnvSafe(key:str)->str:
    val = os.getenv(key)
    if val == None:
        raise Exception(f'Could not find {key} in root .env')
    return val

def deleteFile(path:str):
    if platform == "win32": #windows (any ver)
        subprocess.run(["del","Q", path])
    elif platform == "darwin": #macos
        subprocess.run(["rm",path])
    