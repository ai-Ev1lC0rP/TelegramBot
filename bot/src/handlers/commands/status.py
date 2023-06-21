from bot.src.start import Update, CallbackContext
from bot.src.utils.constants import constant_db_model, constant_db_chat_mode, constant_db_api, constant_db_tokens
async def handle(update: Update, context: CallbackContext, paraprops=None):
    from bot.src.utils.proxies import obtener_contextos as oc, parametros, db, ParseMode, config, model_cache, api_cache, chat_mode_cache
    chat, lang =  await oc(update)
    mododechat_actual, api_actual, modelo_actual, checked_image_api, checked_imaginepy_styles, _ = await parametros(chat, lang, update)
    tokens_actual = await db.get_dialog_attribute(chat, f'{constant_db_tokens}')

    nombreconfig=config.lang["metagen"]["configuracion"][lang]
    api=config.lang["metagen"]["api"][lang]
    nombreapi=config.api["info"][api_actual]["name"]
    apimagen=config.lang["metagen"]["image_api"][lang]
    nombreapimagen=config.api["info"][checked_image_api]["name"]
    imaginestyle=config.lang["metagen"]["imaginepy_actual_style"][lang]
    modelo=config.lang["metagen"]["modelo"][lang]
    nombremodelo=config.model["info"][modelo_actual]["name"]
    tokensmax=config.lang["metagen"]["tokensmax"][lang]
    modeltokens=config.model["info"][modelo_actual]["max_tokens"]
    chatmode=config.lang["metagen"]["chatmode"][lang]
    nombrechatmode=config.chat_mode["info"][mododechat_actual]["name"][lang]
    tokens=config.lang["metagen"]["tokens"][lang]
    textoprimer="""{nombreconfig}:\n\n{api}: {nombreapi} | {modelo}: {nombremodelo}\n{tokens}: {tokens_actual} / {modeltokens}\n{chatmode}: {nombrechatmode}\n{apimagen}: {apimageactual}"""
    if checked_image_api =="imaginepy": textoprimer += "\n{imaginestyle}: {imagineactual}"
    text = f'{textoprimer.format(nombreconfig=nombreconfig, api=api, nombreapi=nombreapi,modelo=modelo,nombremodelo=nombremodelo, tokensmax=tokensmax,modeltokens=modeltokens,chatmode=chatmode, nombrechatmode=nombrechatmode,tokens=tokens, tokens_actual=tokens_actual,apimagen=apimagen, apimageactual=nombreapimagen,imaginestyle=imaginestyle, imagineactual=checked_imaginepy_styles)}'

    if paraprops: return text
    await update.effective_chat.send_message(text, parse_mode=ParseMode.MARKDOWN)
