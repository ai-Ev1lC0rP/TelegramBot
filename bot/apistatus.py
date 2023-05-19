import requests
import apis.opengpt.chatbase as chatbase
import apis.gpt4free as g4f
import apis.gpt4free.foraneo.you as you
import config

def estadosapi():
    vivas = []
    num_errores = 0
    test=False
    if test != True:
        for recorrido in config.api["available_api"]:
            url = config.api["info"][recorrido]["url"]
            key = config.api["info"][recorrido].get("key", "")
            headers = {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + key,
            }

            json_data = {
                'model': 'gpt-3.5-turbo',
                'messages': [
                    {
                        'role': 'user',
                        'content': 'say pong',
                    },
                ],
            }

            try:
                if recorrido == "chatbase":
                    response = chatbase.GetAnswer(messages="say pong")
                elif recorrido == "g4f":
                    provider_name = "Ails"
                    provider = getattr(g4f.Providers, provider_name)
                    # streamed completion
                    response = g4f.ChatCompletion.create(provider=provider, model='gpt-3.5-turbo', messages="say pong", stream=True)
                elif recorrido == "you":
                    response = you.Completion.create(
                        prompt="say pong",
                        detailed=False,
                        include_links=False, )
                    response = dict(response)
                else:
                    response = requests.post(f'{url}/chat/completions', headers=headers, json=json_data, timeout=10)
                if isinstance(response, str):
                    if recorrido == "chatbase" or recorrido == "g4f" or recorrido == "you":
                        #if porque los mamaverga filtran la ip en el mensaje
                        if "API rate limit exceeded" in response:
                            print("límite de API en chatbase!")
                        vivas.append(recorrido)
                    else:
                        num_errores += 1
                elif response.status_code == 200:
                    vivas.append(recorrido)
                else:
                    num_errores += 1
            except requests.exceptions.RequestException as e:
                num_errores += 1
    else:
        vivas = config.api["available_api"]
    print(f"Conexión exitosa con {len(vivas)}, malas: {num_errores}")
    return vivas
