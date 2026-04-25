from google import genai

client = genai.Client(api_key="AIzaSyAUi22VHgDt1XApOA4TJmpprxWErLlCLrA")

for m in client.models.list():
    print(m.name)
