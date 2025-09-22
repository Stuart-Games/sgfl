from pathlib import Path
import os
import subprocess

def getFileURI(name: str)->str:
    cwd = Path.cwd()
    return str(cwd) + "/" + name

def getAbsoluteFileURI(name:str)->str:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(BASE_DIR,name)

def runLuauFile(name:str):
    fileURI = getAbsoluteFileURI(name)
    jsonPath = getAbsoluteFileURI("json/default.assets.json")
    modulePath = getAbsoluteFileURI("lua/module.luau")
    subprocess.run(["lune","run",fileURI,jsonPath,modulePath])

def getEnvSafe(key:str)->str:
    val = os.getenv(key)
    if val == None:
        raise Exception(f'Could not find {key} in root .env')
    return val

def deleteFile(path:str):
    if os.path.exists(path):
        os.remove(path)

PLACE_FILE_PATH = getFileURI("Place.rbxlx")
ASSET_CONFIG_FILE_PATH = getFileURI("assets.json")