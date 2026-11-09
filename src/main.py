import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests


# ============================================================
# CONFIGURAÇÃO
# ============================================================

TWITCH_API = "https://api.twitch.tv/helix"
TWITCH_AUTH_URL = "https://id.twitch.tv/oauth2/token"

CHANNEL = "jotta"

CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

DISCORD_WEBHOOK = os.getenv("DISCORD_CLIP_WEBHOOK_URL")
KICK_WEBHOOK = os.getenv("DISCORD_KICK_CLIP_WEBHOOK_URL")
STATUS_WEBHOOK = os.getenv("DISCORD_STATUS_WEBHOOK_URL")

STATE_FILE = "data/state.json"

# Procuramos clips dentro das últimas 24 horas.
SEARCH_WINDOW_HOURS = 24

# Depois, dentro desses clips, só enviamos os que foram
# criados nos últimos 15 minutos.
RECENT_WINDOW_MINUTES = 15

# Número máximo de páginas.
MAX_PAGES = 10


# ============================================================
# TEMPO
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def format_time(dt):
    return dt.strftime(
        "%d/%m/%Y %H:%M:%S UTC"
    )


# ============================================================
# ESTADO
# ============================================================

def load_state():

    if not os.path.exists(STATE_FILE):

        return {
            "seen_clips": []
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            state = json.load(file)

        state.setdefault(
            "seen_clips",
            []
        )

        return state

    except (ValueError, OSError) as error:
        raise RuntimeError("Não foi possível ler o histórico de clips.") from error


def save_state(state):

    os.makedirs(
        os.path.dirname(STATE_FILE) or ".",
        exist_ok=True
    )

    with open(
        STATE_FILE + ".tmp",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False
        )

    os.replace(STATE_FILE + ".tmp", STATE_FILE)


# ============================================================
# TWITCH AUTH
# ============================================================

def get_access_token():

    response = requests.post(
        TWITCH_AUTH_URL,
        params={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials"
        },
        timeout=30
    )

    print(
        f"🔐 Twitch OAuth: HTTP {response.status_code}"
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"Twitch OAuth falhou: "
            f"{response.text}"
        )

    return response.json()["access_token"]


# ============================================================
# TWITCH API
# ============================================================

def twitch_get(
    endpoint,
    token,
    params
):

    response = requests.get(
        f"{TWITCH_API}/{endpoint}",
        headers={
            "Client-ID": CLIENT_ID,
            "Authorization": f"Bearer {token}"
        },
        params=params,
        timeout=30
    )

    print(
        f"🌐 Twitch {endpoint}: "
        f"HTTP {response.status_code}"
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"Twitch API falhou: "
            f"HTTP {response.status_code}: "
            f"{response.text}"
        )

    return response.json()


def get_channel_id(token):

    data = twitch_get(
        "users",
        token,
        {
            "login": CHANNEL
        }
    )

    users = data.get(
        "data",
        []
    )

    if not users:

        raise RuntimeError(
            f"O canal '{CHANNEL}' não foi encontrado."
        )

    return users[0]["id"]


# ============================================================
# OBTER CLIPS DAS ÚLTIMAS 24 HORAS
# ============================================================

def get_last_24h_clips(
    token,
    channel_id
):

    now = now_utc()

    start_time = (
        now
        - timedelta(
            hours=SEARCH_WINDOW_HOURS
        )
    )

    print(
        f"📅 Procurar clips desde: "
        f"{format_time(start_time)}"
    )

    print(
        f"📅 Até: "
        f"{format_time(now)}"
    )

    all_clips = []

    cursor = None

    for page in range(
        1,
        MAX_PAGES + 1
    ):

        params = {

            "broadcaster_id": channel_id,

            # IMPORTANTE:
            # A Twitch vai procurar directamente
            # dentro das últimas 24 horas.
            "started_at": start_time.isoformat(),

            "ended_at": now.isoformat(),

            "first": 100
        }

        if cursor:

            params["after"] = cursor

        data = twitch_get(
            "clips",
            token,
            params
        )

        clips = data.get(
            "data",
            []
        )

        print(
            f"📦 Página {page}: "
            f"{len(clips)} clips"
        )

        all_clips.extend(
            clips
        )

        pagination = data.get(
            "pagination",
            {}
        )

        cursor = pagination.get(
            "cursor"
        )

        if not cursor:

            break

    return all_clips


def discord_post(webhook, payload):
    payload["allowed_mentions"] = {"parse": []}
    for attempt in range(1, 6):
        try:
            response = requests.post(
                webhook,
                params={"with_components": "true"} if payload.get("components") else None,
                json=payload,
                timeout=30,
            )
        except requests.RequestException:
            # Não imprimir a exceção: pode conter o URL secreto do webhook.
            print("Discord: falha de ligação; envio não confirmado.")
            return False
        if response.status_code in (200, 204):
            return True
        print(f"Discord: HTTP {response.status_code}")
        if response.status_code == 429:
            try:
                delay = float(response.json().get("retry_after", 2 ** attempt)) + 0.5
            except (ValueError, TypeError, AttributeError):
                delay = 2 ** attempt
            # Limitar o tempo de espera quando o Discord impõe um limite.
            if delay > 60:
                return False
        elif response.status_code >= 500:
            delay = min(2 ** attempt, 30)
        else:
            return False
        if attempt < 5:
            time.sleep(max(0, delay))
    return False


def send_check_status(platform, clips_count=None, skipped=False):
    """Envia uma mensagem simples para o webhook de verificações.

    Uma mensagem por plataforma, mesmo quando não existem clips novos.
    Nunca interfere com o envio normal dos clips.
    """
    if not STATUS_WEBHOOK:
        return

    checked_at = now_utc().astimezone(ZoneInfo("Europe/Lisbon"))
    hour = checked_at.strftime("%H:%M")
    if skipped:
        message = (
            f"⚪ {platform}: verificação não realizada às {hour} "
            "(hora de Portugal). Plataforma desativada."
        )
    elif clips_count is None:
        message = (
            f"🔴 {platform}: a verificação falhou às {hour} "
            "(hora de Portugal). Não foi possível confirmar quantos clips novos existem."
        )
    else:
        message = (
            f"🟢 {platform}: verificação realizada às {hour} "
            f"(hora de Portugal). Clips novos detetados: {clips_count}."
        )

    if not discord_post(STATUS_WEBHOOK, {
        "username": "JOTTA Clip Watcher | Verificações",
        "content": message,
    }):
        print(f"{platform}: não foi possível enviar o relatório de verificação.")


def send_to_discord(clip, webhook=None, platform="Twitch"):
    # O formato é o mesmo em ambas as plataformas: título, criador,
    # visualizações, data e botão, sem imagem nem mensagem adicional.
    is_kick = platform == "Kick"
    destination = DISCORD_WEBHOOK if webhook is None else webhook
    published_at = parse_time(clip["created_at"]).astimezone(timezone.utc)
    embed = {
        "author": {"name": "🎬 Novo clip detetado!"},
        "title": (clip.get("title") or "Novo clip do JOTTA")[:256],
        "url": clip["url"],
        "color": 0x53FC18 if is_kick else 0x9146FF,
        "fields": [
            {
                "name": "✂️ Criado por",
                "value": str(clip.get("creator_name") or "Desconhecido")[:1024],
                "inline": True,
            },
            {
                "name": "👁️ Visualizações",
                "value": str(clip.get("view_count", 0)),
                "inline": True,
            },
            {
                "name": "🕒 Publicado",
                "value": published_at.strftime("%d/%m/%Y às %H:%M UTC"),
                "inline": False,
            },
        ],
        "footer": {"text": f"JOTTA {platform} Clip Watcher"},
    }

    return discord_post(destination, {
        "username": f"JOTTA {platform} Clip Watcher",
        "embeds": [embed],
        "components": [{
            "type": 1,
            "components": [{
                "type": 2,
                "style": 5,
                "label": "Abrir clip",
                "url": clip["url"],
            }],
        }],
    })


def get_twitch_clips():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Configura TWITCH_CLIENT_ID e TWITCH_CLIENT_SECRET.")
    token = get_access_token()
    return get_last_24h_clips(token, get_channel_id(token))


def get_kick_clips():
    """Clips recentes do canal na API utilizada pelo site da Kick.

    Este endpoint não pertence à API oficial e pode mudar ou bloquear
    pedidos automáticos. Não é necessária uma chave API adicional.
    """
    clips = []
    found_ids = set()
    cursors = set()
    cursor = None
    oldest_relevant = now_utc() - timedelta(hours=SEARCH_WINDOW_HOURS)

    for page in range(1, MAX_PAGES + 1):
        params = {"sort": "date", "time": "day"}
        if cursor:
            params["cursor"] = cursor
        try:
            response = requests.get(
                f"https://kick.com/api/v2/channels/{CHANNEL}/clips",
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; JOTTAClipWatcher/1.0)",
                },
                timeout=30,
            )
        except requests.RequestException as error:
            raise RuntimeError("Kick: falha de ligação à API.") from error

        print(f"🌐 Kick clips página {page}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise RuntimeError(
                f"Kick: API devolveu HTTP {response.status_code}; "
                "pode estar indisponível ou a bloquear pedidos automáticos."
            )
        try:
            data = response.json()
        except ValueError as error:
            raise RuntimeError("Kick: resposta inválida da API.") from error
        if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
            raise RuntimeError("Kick: formato inesperado na resposta de clips.")

        reached_old_clips = False
        for item in data["clips"]:
            if not isinstance(item, dict) or not item.get("id") or not item.get("created_at"):
                continue
            clip_id = str(item["id"])
            if clip_id in found_ids:
                continue
            found_ids.add(clip_id)
            created_at = parse_time(item["created_at"])
            if created_at < oldest_relevant:
                reached_old_clips = True
                continue
            creator = item.get("creator") or {}
            if isinstance(creator, dict):
                creator = creator.get("username") or creator.get("name")
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith("https://kick.com/"):
                url = f"https://kick.com/{CHANNEL}?clip={clip_id}"
            clips.append({
                "id": clip_id,
                "title": item.get("title") or "Novo clip do JOTTA",
                "url": url,
                "creator_name": creator or "Desconhecido",
                "view_count": item.get("view_count", item.get("views", 0)),
                "created_at": item["created_at"],
            })

        if reached_old_clips:
            return clips
        next_cursor = data.get("nextCursor") or data.get("next_cursor")
        if not next_cursor:
            return clips
        if next_cursor in cursors:
            raise RuntimeError("Kick: cursor de paginação repetido.")
        cursors.add(next_cursor)
        cursor = next_cursor

    raise RuntimeError("Kick: limite de páginas atingido; pesquisa incompleta.")


def process_kick_clips(state):
    now = now_utc()
    cutoff = now - timedelta(minutes=RECENT_WINDOW_MINUTES)
    history = state.setdefault("kick_seen_clips", [])
    seen = set(history)
    new_clips = {}

    for clip in get_kick_clips():
        created_at = parse_time(clip["created_at"])
        if clip["id"] in seen:
            reason = "já enviado"
        elif created_at < cutoff:
            reason = "fora do intervalo de 15 minutos"
        elif created_at > now:
            reason = "data posterior ao início da verificação"
        else:
            reason = "novo, elegível para envio"
            new_clips[clip["id"]] = clip
        print(f"Kick clip {clip['id']}: criado em {created_at.isoformat()}; {reason}.")

    clips = sorted(new_clips.values(), key=lambda clip: parse_time(clip["created_at"]))
    print(f"Kick: {len(clips)} clips novos nos últimos {RECENT_WINDOW_MINUTES} minutos.")
    if not clips:
        return 0

    successful = True
    for clip in clips:
        if send_to_discord(clip, webhook=KICK_WEBHOOK, platform="Kick"):
            history.append(clip["id"])
            state["kick_seen_clips"] = history[-5000:]
            save_state(state)
        else:
            successful = False
        time.sleep(1)
    if not successful:
        raise RuntimeError("Kick: um ou mais envios para o Discord falharam.")
    return len(clips)


def process_twitch_clips(state):
    now = now_utc()
    cutoff = now - timedelta(minutes=RECENT_WINDOW_MINUTES)
    # Manter a chave original evita reenviar clips já publicados.
    history = state.setdefault("seen_clips", [])
    seen = set(history)
    new_clips = {}
    for clip in get_twitch_clips():
        created_at = parse_time(clip["created_at"])
        if clip["id"] in seen:
            reason = "já enviado"
        elif created_at < cutoff:
            reason = "fora do intervalo de 15 minutos"
        elif created_at > now:
            reason = "data posterior ao início da verificação"
        else:
            reason = "novo, elegível para envio"
            new_clips[clip["id"]] = clip
        print(f"Twitch clip {clip['id']}: criado em {created_at.isoformat()}; {reason}.")
    clips = sorted(new_clips.values(), key=lambda clip: parse_time(clip["created_at"]))
    print(f"Twitch: {len(clips)} clips novos nos últimos {RECENT_WINDOW_MINUTES} minutos.")
    if not clips:
        return 0
    successful = True
    for clip in clips:
        if send_to_discord(clip):
            history.append(clip["id"])
            state["seen_clips"] = history[-5000:]
            # Guardar cada envio confirmado, mesmo se o seguinte falhar.
            save_state(state)
        else:
            successful = False
        time.sleep(1)
    if not successful:
        raise RuntimeError("Twitch: um ou mais envios para o Discord falharam.")
    return len(clips)


def main():
    state = load_state()
    twitch_error = None
    kick_error = None
    twitch_count = None
    kick_count = None

    if not STATUS_WEBHOOK:
        print("Relatórios desativados: configura DISCORD_STATUS_WEBHOOK_URL.")

    # Uma falha na Kick não impede o processamento da Twitch, nem o inverso.
    try:
        if not DISCORD_WEBHOOK:
            raise RuntimeError("DISCORD_CLIP_WEBHOOK_URL não configurado.")
        twitch_count = process_twitch_clips(state)
    except Exception as error:
        twitch_error = error
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f"Twitch: falhou: {detail}")
    finally:
        save_state(state)
        send_check_status("Twitch", twitch_count if twitch_error is None else None)

    if KICK_WEBHOOK:
        try:
            kick_count = process_kick_clips(state)
        except Exception as error:
            kick_error = error
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            print(f"Kick: falhou: {detail}")
        finally:
            save_state(state)
            send_check_status("Kick", kick_count if kick_error is None else None)
    else:
        print("Kick desativada: configura DISCORD_KICK_CLIP_WEBHOOK_URL.")
        send_check_status("Kick", skipped=True)

    if twitch_error:
        raise twitch_error
    if kick_error:
        raise RuntimeError("Kick: verificação ou envio falhou.") from kick_error


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"CHECK FALHOU: {type(error).__name__}")
        sys.exit(1)
