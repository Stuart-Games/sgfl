import json
import subprocess
import requests
from sys import platform
from .util import *

def startPlace(pull:bool):
    placeId = getEnvSafe("PLACE_ID")
    universeId = getEnvSafe("UNIVERSE_ID")
    publishKey = getEnvSafe("PUBLISH_KEY")
    userId = getEnvSafe("USER_ID")

    #pull and build
    if pull:
        subprocess.run(["git", "pull"])

    runLuauFile("lua/build.luau")

    #make correct publish req to roblox
    url = f'https://apis.roblox.com/universes/v1/{universeId}/places/{placeId}/versions?versionType=Published'
    headers = {"x-api-key":publishKey,"Content-Type":"application/xml"}

    with open(PLACE_FILE_PATH,'rb') as f:
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

    deleteFile(PLACE_FILE_PATH)
    deleteFile("sourcemap.json")
    subprocess.run(["code", "."], shell=True)
    subprocess.run(["rojo", "serve"])

def savePlace():
    placeId = getEnvSafe("PLACE_ID")
    downloadKey = getEnvSafe("DOWNLOAD_KEY")


    #make correct publish req to roblox
    url = f'https://apis.roblox.com/asset-delivery-api/v1/assetId/{placeId}'
    headers = {"x-api-key":downloadKey}

    res = requests.get(url,headers=headers)

    downloadUrl = json.loads(res.text)['location']

    if downloadUrl == None:
        print("Couldn't get download url to download place file")

    placeData = requests.get(downloadUrl).content

    #write to place file
    with open(PLACE_FILE_PATH,'wb') as f:
        f.write(placeData)

    runLuauFile("lua/importAssets.luau")

    deleteFile(PLACE_FILE_PATH)
    deleteFile("sourcemap.json")
    print("Wrote place data to the local file system.")