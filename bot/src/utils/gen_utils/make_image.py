async def gen(self, prompt):
    from bot.src.utils import config
    import secrets
    import openai
    if self.api not in config.api["available_imagen"]:
        index = secrets.randbelow(len(config.api["available_imagen"]))
        self.api = config.api["available_imagen"][index]
    openai.api_key = config.api["info"][self.api]["key"]
    openai.api_base = config.api["info"][self.api]["url"]
    r = await openai.Image.acreate(prompt=prompt, n=config.n_images, size="1024x1024")
    image_urls = [item.url for item in r.data]
    return image_urls