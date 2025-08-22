import json
import subprocess
import requests
from sys import platform
from .luauScripts import build,importAssets
from .util import *

def startPlace(userId:str,placeId:str,universeId:str,publishKey:str,placeFilePath:str,pull:bool):
    #pull and build
    if pull:
        subprocess.run(["git", "pull"])
        
    subprocess.run(["lune","run","-"],input=build,text=True)

    #make correct publish req to roblox
    url = f'https://apis.roblox.com/universes/v1/{universeId}/places/{placeId}/versions?versionType=Published'
    headers = {"x-api-key":publishKey,"Content-Type":"application/xml"}

    with open(placeFilePath,'rb') as f:
        bin = f.read()

    res = requests.post(url,headers=headers,data=bin)

    if res.status_code != 200:
        print(res.text)

    placeOpenString=f"roblox-studio:1+userId:{userId}+task:EditPlace+placeId:{placeId}+universeId:{universeId}"

    #open studio (generic window)
    if platform == "win32": #windows (any ver)
        subprocess.run(["start", placeOpenString], shell=True)
    elif platform == "darwin": #macos
        subprocess.run(["open", placeOpenString])

    deleteFile(placeFilePath)
    deleteFile("sourcemap.json")
    subprocess.run(["code", "."], shell=True)
    subprocess.run(["rojo", "serve"])

def savePlace(placeId:str,downloadKey:str,placeFilePath:str):
    #make correct publish req to roblox
    url = f'https://apis.roblox.com/asset-delivery-api/v1/assetId/{placeId}'
    headers = {"x-api-key":downloadKey}

    res = requests.get(url,headers=headers)

    downloadUrl = json.loads(res.text)['location']

    if downloadUrl == None:
        print("Couldn't get download url to download place file")

    placeData = requests.get(downloadUrl).content

    #write to place file
    with open(placeFilePath,'wb') as f:
        f.write(placeData)

    subprocess.run(["lune", "run","-"],input=importAssets,text=True)

    deleteFile(placeFilePath)
    deleteFile("sourcemap.json")
    print("Wrote place data to the local file system.")