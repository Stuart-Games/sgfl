from pathlib import Path
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
    os.remove(path)
    