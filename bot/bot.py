import os
import logging
import asyncio
import traceback
import html
import json
import tempfile
from pathlib import Path
from datetime import datetime
import telegram
from telegram import (
    Update,
    InputMediaDocument,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode, ChatAction, ChatType
import config
import database
import openai_utils
import schedule
import signal
import sys
import re

# setup

running = True

db = database.Database()

logger = logging.getLogger(__name__)

user_semaphores = {}
user_tasks = {}

HELP_MESSAGE = """Comandos:
⚪ /new - Iniciar nuevo diálogo.
⚪ /img - Genera imágenes según lo que escribas.
⚪ /retry - Regenera la última respuesta del bot.
⚪ /chat_mode - Seleccionar el modo de conversación.
⚪ /model - Mostrar configuración de API.
⚪ /api - Mostrar APIs.
⚪ /help – Mostrar este mensaje de nuevo.

🎨 Generar textos para imágenes con <b>🖼️ Generar imágenes</b> /chat_mode
👥 Añadir el bot a un <b>grupo</b>: /help_group_chat
🎤 Puedes enviar <b>Mensajes de voz</b> en lugar de texto.
📖 Envía <b>documentos</b> o <b>links</b> para <b>analizarlos</b> junto al bot!
"""

HELP_GROUP_CHAT_MESSAGE = """Puedes añadir un bot a cualquier <b>chat de grupo</b> para ayudar y entretener a sus participantes.

Instrucciones (ver <b>vídeo</b> más abajo)
1. Añade el bot al chat de grupo
2. Conviértelo en <b>administrador</b>, para que pueda ver los mensajes (el resto de derechos se pueden restringir)
3. Eres increíble!

To get a reply from the bot in the chat – @ <b>tag</b> it or <b>reply</b> to its message.
Por ejemplo: "{bot_username} escribe un poema sobre Telegram"
"""

apis_vivas = []
async def obtener_vivas():
    print("Se ejecutó chequeo de APIs")
    global apis_vivas
    from apistatus import estadosapi
    apis_vivas = await estadosapi()
    print(apis_vivas)

async def run_schedule():
    while True:
        schedule.run_pending()
        await asyncio.sleep(1)

def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]

async def user_check(update: Update, context: CallbackContext, user=None):
    if user is None:
        if update.message:
            user = update.message.from_user
        elif update.callback_query:
            user = update.callback_query.from_user
        else:
            await update.message.reply_text(f"Ocurrió un error gestionando un nuevo diálogo.")
    await register_user_if_not_exists(update, context, user)
    return user
        
async def register_user_if_not_exists(update: Update, context: CallbackContext, user=None):
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name= user.last_name
        )
        db.start_new_dialog(user.id)

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)

    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    if db.get_user_attribute(user.id, "current_model") is None:
        db.set_user_attribute(user.id, "current_model", config.model["available_model"][0])
        
    if db.get_user_attribute(user.id, "current_api") is None:
        db.set_user_attribute(user.id, "current_api", apis_vivas[0])


async def is_bot_mentioned(update: Update, context: CallbackContext):
     try:
         message = update.message

         if message.chat.type == "private":
             return True

         if message.text is not None and ("@" + context.bot.username) in message.text:
             return True

         if message.reply_to_message is not None:
             if message.reply_to_message.from_user.id == context.bot.id:
                 return True
     except:
         return True
     else:
         return False

async def start_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    await new_dialog_handle(update, context)

    reply_text = "Hola! Soy <b>ChatGPT</b> bot implementado con la API de OpenAI.🤖\n\n"
    reply_text += HELP_MESSAGE

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)

async def help_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)

async def help_group_chat_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    
    text = HELP_GROUP_CHAT_MESSAGE.format(bot_username="@" + context.bot.username)

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    await update.message.reply_video(config.help_group_chat_video_path)

async def retry_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    
    dialog_messages = db.get_dialog_messages(user.id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("No hay mensaje para reintentar 🤷‍♂️")
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user.id, dialog_messages, dialog_id=None)  # last message was removed from the context
    db.set_user_attribute(user.id, "last_interaction", datetime.now())
    await message_handle(update, context, message=last_dialog_message["user"])

async def message_handle(update: Update, context: CallbackContext, message=None):
    user = await user_check(update, context)
    chat_mode = db.get_user_attribute(user.id, "current_chat_mode")
    current_model = db.get_user_attribute(user.id, "current_model")
    dialog_messages = db.get_dialog_messages(user.id, dialog_id=None)
    # check if bot was mentioned (for groups)
    if not await is_bot_mentioned(update, context):
        return
    # new dialog timeout
    raw_msg = message or update.message or update.callback_query.message
    _message = raw_msg.text
    try:
        if raw_msg.entities:
            urls = []
            _message = raw_msg.text
            
            entities = raw_msg.parse_entities()
            for entity in raw_msg.entities:
                if entity.type == 'url':
                    url_add = raw_msg.text[entity.offset:entity.offset+entity.length]
                    if "http://" in url_add or "https://" in url_add:
                        urls.append(raw_msg.text[entity.offset:entity.offset+entity.length])
                        _message[:entity.offset] + _message[entity.offset+entity.length:]
                    else:
                        pass
            if urls:
                await url_handle(update, context, urls)
                return
    except AttributeError:
        pass
        
    if (datetime.now() - db.get_user_attribute(user.id, "last_interaction")).seconds > config.dialog_timeout and len(dialog_messages) > 0:
        if config.timeout_ask == "True":
            await ask_timeout_handle(update, context, _message)
            return
        else:
            await new_dialog_handle(update, context)
            await update.effective_chat.send_message(f"Starting new dialog due to timeout (<b>{config.chat_mode['info'][chat_mode]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)

    chat = None
    #remove bot mention (in group chats)
    if raw_msg != None:
        if raw_msg.chat.type != "private":
            _message = _message.replace("@" + context.bot.username, "").strip()
            chat = raw_msg.chat

    if await is_previous_message_not_answered_yet(update, context): return
    if chat_mode == "artist":
        await generate_image_handle(update, context, message=_message)
        return
        
    async with user_semaphores[user.id]:
        task = asyncio.create_task(message_handle_fn(update, context, _message, chat, dialog_messages, chat_mode, current_model, user))
        user_tasks[user.id] = task
    
        try:
            await task
        except asyncio.CancelledError:
            await update.effective_chat.send_message("✅ Cancelado", parse_mode=ParseMode.HTML)
        else:
            pass
        finally:
            if user.id in user_tasks:
                del user_tasks[user.id]
        
async def message_handle_fn(update, context, _message, chat, dialog_messages, chat_mode, current_model, user):
    # in case of CancelledError
    try:
        # send placeholder message to user
        placeholder_message = await update.effective_chat.send_message("🤔")
        # send typing action
        if chat:
            await chat.send_action(ChatAction.TYPING)
        if _message is None or len(_message) == 0:
            await update.effective_chat.send_message("🥲 You sent <b>empty message</b>. Please, try again!", parse_mode=ParseMode.HTML)
            return
        parse_mode = {
            "html": ParseMode.HTML,
            "markdown": ParseMode.MARKDOWN
        }[config.chat_mode["info"][chat_mode]["parse_mode"]]
        chatgpt_instance = openai_utils.ChatGPT(model=current_model)
        gen = chatgpt_instance.send_message(_message, user.id, dialog_messages=dialog_messages, chat_mode=chat_mode)     
        prev_answer = ""
        async for status, gen_answer in gen:                                                         
            answer = gen_answer[:4096]  # telegram message limit                                     
                                                                                                    
            # update only when 100 new symbols are ready                                             
            if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":                    
                continue                                                                             
            try:                                                                                     
                await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=parse_mode)                                
            except telegram.error.BadRequest as e:                                                   
                if str(e).startswith("Message is not modified"):                                     
                    continue                                                                         
                else:                                                                                
                    await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id)                                                       
            await asyncio.sleep(0.5)  # wait a bit to avoid flooding                                 
                                                                                                    
            prev_answer = answer
        # update user data
        new_dialog_message = {"user": _message, "bot": answer, "date": datetime.now()}
        await add_dialog_message(update, context, new_dialog_message)
        db.set_user_attribute(user.id, "last_interaction", datetime.now())
        
    except Exception as e:
        error_text = f"Error: {e}"
        logger.error(error_text)
        await update.effective_chat.send_message(error_text)
        return
    if chat_mode == "imagen":
        await generate_image_handle(update, context, message=answer)
        return
    
                
async def add_dialog_message(update: Update, context: CallbackContext, new_dialog_message):
    user = await user_check(update, context)
    db.set_dialog_messages(
        user.id,
        db.get_dialog_messages(user.id, dialog_id=None) + [new_dialog_message],
        dialog_id=None
    )
    return

async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    semaphore = user_semaphores.get(user.id)
    if semaphore and semaphore.locked():
        text = "⏳ Por favor <b>espera</b> una respuesta al mensaje anterior\n"
        text += "O puedes /cancel"
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    else:
        return False

async def clean_text(doc, name):
    doc = re.sub(r'^\n', '', doc) 
    doc = re.sub(r'\n+', r' ', doc) # Reemplaza saltos de línea dentro de párrafos por un espacio  
    doc = re.sub(r' {2,}', ' ', doc) # Reemplaza dos o más espacios con uno solo
    doc = re.sub(r'\s+', ' ', doc).strip()
    #doc = "\n".join(line.strip() for line in doc.split("\n"))
    doc_text = f'[{name}: {doc}]'
    return doc_text

async def url_handle(update, context, urls):
    user = await user_check(update, context)
    import requests
    from bs4 import BeautifulSoup
    import warnings
    warnings.filterwarnings("ignore")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36 Edg/91.0.864.54"
    }
    for url in urls:
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            if len(response.content) > config.url_max_size:
                raise Exception("URL response muy grande")
            soup = BeautifulSoup(response.content, "html.parser")
            body_tag = soup.body
            if body_tag:
                doc = body_tag.get_text('\n')
            else:
                # Si no hay etiqueta <body>, obtener todo el contenido de la página
                doc = soup.get_text('\n')
            doc_text = await clean_text(doc, name=url)
            new_dialog_message = {"url": doc_text, "user": ".", "date": datetime.now()}
            await add_dialog_message(update, context, new_dialog_message)
            text = f"Anotado 📝 ¿Qué quieres saber de la página?"
        except Exception as e:
            logging.error(f"Error al obtener el contenido de la página web: {e}")
            text = "Error al obtener el contenido de la página web."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    db.set_user_attribute(user.id, "last_interaction", datetime.now())

async def document_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return
    if await is_previous_message_not_answered_yet(update, context): return
    
    document = update.message.document
    file_size_mb = document.file_size / (1024 * 1024)
    if file_size_mb <= config.file_max_size:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir = Path(tmp_dir)
            ext = document.file_name.split(".")[-1]
            doc_path = tmp_dir / Path(document.file_name)
            # download
            doc_file = await context.bot.get_file(document.file_id)
            await doc_file.download_to_drive(doc_path)
            
            if "pdf" in ext:
                pdf_file = open(doc_path, 'rb')
                import PyPDF2
                read_pdf = PyPDF2.PdfReader(pdf_file)
                doc = ''
                paginas = len(read_pdf.pages)
                if paginas > config.pdf_page_lim:
                    text = f"😬 ¡El documento se excede por {paginas - config.pdf_page_lim} páginas! Se leerán las primeras {config.pdf_page_lim} páginas."
                    paginas = config.pdf_page_lim - 1
                    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
                for i in range(paginas):
                    text = read_pdf.pages[i].extract_text()
                    text = text.replace(".\n", "|n_parraf|")  
                    paras = text.split("|n_parraf|")
                    parafo_count = 1
                    for para in paras:
                        if len(para) > 3:
                            doc += f'Pagina{i+1}_Parrafo{parafo_count}: {para}\n\n'      
                            parafo_count += 1
            else:
                with open(doc_path, 'r') as f:
                    doc = f.read()
            doc_text = await clean_text(doc, name=document.file_name)
            new_dialog_message = {"documento": doc_text, "user": ".", "date": datetime.now()}
            await add_dialog_message(update, context, new_dialog_message)
            text = f"Anotado 🫡 ¿Qué quieres saber del archivo?"
            db.set_user_attribute(user.id, "last_interaction", datetime.now())
    else:
        text = f"El archivo es demasiado grande ({file_size_mb:.2f} MB). El límite es de {config.file_max_size} MB."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
 
async def transcribe_message_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    # check if bot was mentioned (for group chats) 
    if not await is_bot_mentioned(update, context): 
        return
    if await is_previous_message_not_answered_yet(update, context): return
    # Procesar sea voz o audio         
    if update.message.voice:
        audio = update.message.voice     
    elif update.message.audio:
        audio = update.message.audio
    file_size_mb = audio.file_size / (1024 * 1024)
    if file_size_mb <= config.audio_max_size:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Descargar y convertir a MP3
            tmp_dir = Path(tmp_dir)
            ext = audio.mime_type
            import mimetypes
            ext = mimetypes.guess_extension(ext)
            doc_path = tmp_dir / Path("tempaudio" + ext)
    
            # download
            voice_file = await context.bot.get_file(audio.file_id)
            await voice_file.download_to_drive(doc_path)
    
            # convert to mp3
            mp3_file_path = tmp_dir / "voice.mp3"
            from pydub import AudioSegment
            AudioSegment.from_file(doc_path).export(mp3_file_path, format="mp3")
            
            
            # Transcribir
            with open(mp3_file_path, "rb") as f:
                transcribed_text = await openai_utils.transcribe_audio(user.id, f)  
                
        # Enviar respuesta            
        text = f"🎤: {transcribed_text}"
        db.set_user_attribute(user.id, "last_interaction", datetime.now())
    else:
        text = f'💀 El archivo de audio sobrepasa el limite de {config.audio_max_size} megas!'
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    await message_handle(update, context, message=transcribed_text)

async def generate_image_handle(update: Update, context: CallbackContext, message=None):
    user = await user_check(update, context)
    if message:
        prompt = message
    else:
        if not context.args:
            await update.message.reply_text("Debes escribir algo después del comando /img", parse_mode=ParseMode.HTML)
            return
        else:
            prompt = ' '.join(context.args)
    if prompt == None:
        await update.message.reply_text("No se detectó texto para generar las imágenes 😔", parse_mode=ParseMode.HTML)
        return
        
    await update.message.chat.send_action(action="upload_photo")
    import openai
    try:
        image_urls = await openai_utils.generate_images(prompt, user.id)
    except openai.error.APIError as e:
        if "inappropriate content" in str(e):
            text = "🥲 Tu solicitud <b>no cumple</b> con las políticas de uso de OpenAI.</b> ¿Qué has escrito ahí, eh?"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        else:
            raise
    db.set_user_attribute(user.id, "last_interaction", datetime.now())
    media_group=[]
    for i, image_url in enumerate(image_urls):
        media = InputMediaPhoto(image_url)
        media_group.append(media)
    await update.message.chat.send_action(action="upload_photo")
    await update.message.reply_media_group(media_group)
    
    media_group=[]
    for i, image_url in enumerate(image_urls):
        media = InputMediaDocument(image_url, parse_mode=ParseMode.HTML, filename=f"imagen_{i}.png")
        media_group.append(media)

    await update.message.chat.send_action(action="upload_document")
    await update.message.reply_media_group(media_group)
    
    
async def ask_timeout_handle(update: Update, context: CallbackContext, _message):
    user = await user_check(update, context)

    keyboard = [[
        InlineKeyboardButton("✅", callback_data=f"new_dialog|true"),
        InlineKeyboardButton("❎", callback_data=f"new_dialog|false"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    new_dialog_message = {"user": _message, "date": datetime.now()}
    await add_dialog_message(update, context, new_dialog_message)

    await update.effective_chat.send_message(f"Tiempo sin hablarte! ¿Iniciamos nueva conversación?", reply_markup=reply_markup)


async def answer_timeout_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    query = update.callback_query
    await query.answer()

    new_dialog = query.data.split("|")[1]
    dialog_messages = db.get_dialog_messages(user.id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.effective_chat.send_message("No hay historial. Iniciando uno nuevo 🤷‍♂️")
        await new_dialog_handle(update, context, user.id)
        return
    elif 'bot' in dialog_messages[-1]: # already answered, do nothing
        return
    if new_dialog == "true":
        last_dialog_message = dialog_messages.pop()
        await new_dialog_handle(update, context)
        await message_handle(update, context, message=last_dialog_message["user"])
    else:
        await retry_handle(update, context)
        
async def new_dialog_handle(update: Update, context: CallbackContext, user=None):
    user = await user_check(update, context)
    
    api_actual = db.get_user_attribute(user.id, 'current_api')
    modelo_actual = db.get_user_attribute(user.id, 'current_model')
    mododechat_actual=db.get_user_attribute(user.id, 'current_chat_mode')

    
    # Verificar si hay valores inválidos en el usuario
    if (mododechat_actual not in config.chat_mode["available_chat_mode"] or api_actual not in apis_vivas or modelo_actual not in config.model["available_model"]):
        db.reset_user_attribute(user.id)
        await update.effective_chat.send_message("Tenías un parámetro no válido en la configuración, por lo que se ha restablecido todo a los valores predeterminados.")

    modelos_disponibles=config.api["info"][api_actual]["available_model"]
    apisconimagen=config.api["available_imagen"]
    api_actual_name=config.api["info"][api_actual]["name"]
    
    # Verificar si el modelo actual es válido en la API actual
    if modelo_actual not in modelos_disponibles:
        db.set_user_attribute(user.id, "current_model", modelos_disponibles[0])
        await update.effective_chat.send_message(f'Tu modelo actual no es compatible con la API "{api_actual_name}", por lo que se ha cambiado el modelo automáticamente a "{config.model["info"][db.get_user_attribute(user.id, "current_model")]["name"]}".')
    db.start_new_dialog(user.id)
    db.delete_all_dialogs_except_current(user.id)
    #Bienvenido!
    await update.effective_chat.send_message(f"{config.chat_mode['info'][db.get_user_attribute(user.id, 'current_chat_mode')]['welcome_message']}", parse_mode=ParseMode.HTML)
    db.set_user_attribute(user.id, "last_interaction", datetime.now())

async def cancel_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)

    if user.id in user_tasks:
        db.set_user_attribute(user.id, "last_interaction", datetime.now())
        task = user_tasks[user.id]
        task.cancel()
    else:
        await update.message.reply_text("<i>No hay nada que cancelar...</i>", parse_mode=ParseMode.HTML)

async def get_menu(update: Update, context: CallbackContext, menu_type: str):
    user = await user_check(update, context)
    menu_type_dict = getattr(config, menu_type)
    api_antigua = db.get_user_attribute(user.id, 'current_api')
    if api_antigua not in apis_vivas:
        db.set_user_attribute(user.id, "current_api", apis_vivas[0])
        await update.effective_chat.send_message(f'Tu API actual "{api_antigua}" no está disponible. Por lo que se ha cambiado automáticamente a "{menu_type_dict["info"][db.get_user_attribute(user.id, "current_api")]["name"]}".')
        pass
    modelos_disponibles = config.api["info"][db.get_user_attribute(user.id, "current_api")]["available_model"]
    if db.get_user_attribute(user.id, 'current_model') not in modelos_disponibles:
        db.set_user_attribute(user.id, "current_model", modelos_disponibles[0])
        await update.effective_chat.send_message(f'Tu modelo actual no es compatible con la API actual, por lo que se ha cambiado el modelo automáticamente a "{config.model["info"][db.get_user_attribute(user.id, "current_model")]["name"]}".')
        pass
    if menu_type == "model":
        item_keys = modelos_disponibles
    elif menu_type == "api":
        item_keys = apis_vivas
    else:
        item_keys = menu_type_dict[f"available_{menu_type}"]
        
    current_key = db.get_user_attribute(user.id, f"current_{menu_type}")

    text = "<b>Actual:</b>\n\n" + str(menu_type_dict["info"][current_key]["name"]) + ", " + menu_type_dict["info"][current_key]["description"] + "\n\n<b>Selecciona un " + f"{menu_type}" + " disponible</b>:"

    num_cols = 2
    import math
    num_rows = math.ceil(len(item_keys) / num_cols)
    options = [[menu_type_dict["info"][current_key]["name"], f"set_{menu_type}|{current_key}", current_key] for current_key in item_keys]
    reply_markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(name, callback_data=data) 
                for name, data, selected in options[i::num_rows]
            ]
            for i in range(num_rows)
        ]
    )

    return text, reply_markup

async def chat_mode_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    text, reply_markup = await get_menu(update, user, "chat_mode")
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def chat_mode_callback_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    query = update.callback_query
    await query.answer()
    text, reply_markup = await get_menu(update, context, "chat_mode")
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("El mensaje no se modifica"):
            pass

async def set_chat_mode_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    query = update.callback_query
    await query.answer()
    mode = query.data.split("|")[1]
    db.set_user_attribute(user.id, "current_chat_mode", mode)
    text, reply_markup = await get_menu(update, context, "chat_mode")
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        await update.effective_chat.send_message(f"{config.chat_mode['info'][db.get_user_attribute(user.id, 'current_chat_mode')]['welcome_message']}", parse_mode=ParseMode.HTML)
        db.set_user_attribute(user.id, "last_interaction", datetime.now())
    except telegram.error.BadRequest as e:
        if str(e).startswith("El mensaje no se modifica"):
            pass

async def model_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    text, reply_markup = await get_menu(update, context, "model")
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def model_callback_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    query = update.callback_query
    await query.answer()
    text, reply_markup = await get_menu(update, context, "model")
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("El mensaje no se modifica"):
            pass

async def set_model_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    query = update.callback_query
    await query.answer()
    _, model = query.data.split("|")
    db.set_user_attribute(user.id, "current_model", model)
    text, reply_markup = await get_menu(update, context, "model")
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        db.set_user_attribute(user.id, "last_interaction", datetime.now())
    except telegram.error.BadRequest as e:
        if str(e).startswith("El mensaje no se modifica"):
            pass

async def api_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    text, reply_markup = await get_menu(update, context, "api")
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
async def api_callback_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    query = update.callback_query
    await query.answer()
    text, reply_markup = await get_menu(update, context, "api")
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("El mensaje no se modifica"):
            pass

async def set_api_handle(update: Update, context: CallbackContext):
    user = await user_check(update, context)
    query = update.callback_query
    await query.answer()
    _, api = query.data.split("|")
    db.set_user_attribute(user.id, "current_api", api)
    # check if there is an ongoing dialog
    current_dialog = db.get_user_attribute(user.id, "current_dialog")
    if current_dialog is not None:
        await update.effective_chat.send_message("Por favor, termina tu conversación actual antes de iniciar una nueva.")
        return
    text, reply_markup = await get_menu(update, context, "api")
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        db.set_user_attribute(user.id, "last_interaction", datetime.now())
    except telegram.error.BadRequest as e:
        if str(e).startswith("El mensaje no se modifica"):
            pass

async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Excepción al gestionar una actualización:", exc_info=context.error)
    try:
        # collect error message
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"Excepción al gestionar una actualización\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # # split text into multiple messages due to 4096 character limit
        # for message_chunk in split_text_into_chunks(message, 4096):
        #     try:
        #         await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
        #     except telegram.error.BadRequest:
        #         # answer has invalid characters, so we send it without parse_mode
        #         await context.bot.send_message(update.effective_chat.id, message_chunk)
    except:
        await context.bot.send_message("Algún error en el gestor de errores")

async def post_init(application: Application):
    asyncio.create_task(run_schedule())
    schedule.every().hour.at(":00").do(obtener_vivas)
    await application.bot.set_my_commands([
        BotCommand("/new", "Iniciar un nuevo diálogo"),
        BotCommand("/chat_mode", "Cambia el modo de asistente"),
        BotCommand("/retry", "Re-generar respuesta para la consulta anterior"),
        BotCommand("/model", "Mostrar modelos de API"),
        BotCommand("/api", "Mostrar APIs"),
        BotCommand("/img", "Genera imágenes según lo que escribas"),
        BotCommand("/help", "Ver mensaje de ayuda"),
    ])

def signal_handler(sig, frame):
    global running
    if sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        print("Señal de interrupción recibida. Cerrando el bot...")
        running = False
        sys.exit(0)

def run_bot() -> None:
    global running
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    while running:
        try:
            application = (
                ApplicationBuilder()
                .token(config.telegram_token)
                .concurrent_updates(True)
                .rate_limiter(AIORateLimiter(max_retries=5))
                .post_init(post_init)
                .build()
            )

            # add handlers

            if config.user_whitelist:
                usernames = []
                user_ids = []
                for user in config.user_whitelist:
                    user = user.strip()
                    if user.isnumeric():
                        user_ids.append(int(user))
                    else:
                        usernames.append(user)
                user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids)
            else:
                user_filter = filters.ALL

            application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
            application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
            application.add_handler(CommandHandler("help_group_chat", help_group_chat_handle, filters=user_filter))

            application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
            application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
            application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
            application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))
            application.add_handler(CallbackQueryHandler(answer_timeout_handle, pattern="^new_dialog"))

            application.add_handler(MessageHandler(filters.AUDIO & user_filter, transcribe_message_handle))
            application.add_handler(MessageHandler(filters.VOICE & user_filter, transcribe_message_handle))
            
            application.add_handler(MessageHandler(filters.Document.Category('text/') & user_filter, document_handle))
            
            docfilter = (filters.Document.FileExtension("pdf") | filters.Document.FileExtension("lrc"))
            application.add_handler(MessageHandler(docfilter & user_filter, document_handle))

            application.add_handler(CommandHandler("chat_mode", chat_mode_handle, filters=user_filter))
            application.add_handler(CommandHandler("model", model_handle, filters=user_filter))
            application.add_handler(CommandHandler("api", api_handle, filters=user_filter))
            application.add_handler(CommandHandler("img", generate_image_handle, filters=user_filter))
            
            application.add_handler(CommandHandler('status', obtener_vivas, filters=user_filter))

            application.add_handler(CallbackQueryHandler(chat_mode_callback_handle, pattern="^get_menu"))
            application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

            application.add_handler(CallbackQueryHandler(model_callback_handle, pattern="^get_menu"))
            application.add_handler(CallbackQueryHandler(set_model_handle, pattern="^set_model"))

            application.add_handler(CallbackQueryHandler(api_callback_handle, pattern="^get_menu"))
            application.add_handler(CallbackQueryHandler(set_api_handle, pattern="^set_api"))
            application.add_error_handler(error_handle)

            application.run_polling()

        except Exception as e:
            if not running:
                break
            print(f"Error: {e}. Intentando reconectar en 3 segundos...")
            import time
            time.sleep(3)

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(obtener_vivas())  # Ejecutar obtener_vivas antes de iniciar el bot
    run_bot()