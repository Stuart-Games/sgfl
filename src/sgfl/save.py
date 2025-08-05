import subprocess
import requests
import json
import util

PLACE_FILE_PATH = util.getFileURI("WaterPhysics")

#environment variables
downloadKey = util.getEnvSafe("DOWNLOAD_KEY")
placeId = util.getEnvSafe("PLACE_ID")

#make correct publish req to roblox
url = f'https://apis.roblox.com/asset-delivery-api/v1/assetId/{placeId}'
headers = {"x-api-key":downloadKey}

res = requests.get(url,headers=headers)

downloadUrl = json.loads(res.text)['location']

if downloadUrl == None:
    print("Couldn't get download url to download place file")

print("Requesting place data from Roblox...")
placeData = requests.get(downloadUrl).content

#write to place file
with open(PLACE_FILE_PATH,'wb') as f:
    f.write(placeData)

subprocess.run(["lune", "run","tools/import-assets"])
print("Finished writing place data to the local file system.")
