import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

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
            response = requests.post(webhook, json=payload, timeout=30)
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


def send_interval_message(start_time, end_time, clip_count):
    return discord_post(DISCORD_WEBHOOK, {
        "username": "JOTTA Twitch Clip Watcher",
        "content": (
            "🕐 **JOTTA Twitch Clip Watcher**\n\n"
            f"Clips entre **{start_time:%H:%M:%S}** e **{end_time:%H:%M:%S} UTC**\n\n"
            f"🎯 **{clip_count} clips novos detetados**"
        ),
    })


def send_to_discord(clip):
    return discord_post(DISCORD_WEBHOOK, {
        "username": "JOTTA Twitch Clip Watcher",
        "content": "🎬 **NOVO CLIP DO JOTTA | TWITCH**",
        "embeds": [{
            "title": (clip.get("title") or "Novo clip")[:256],
            "url": clip["url"],
            "color": 0x9146FF,
            "description": (
                f"📺 **Streamer:** {CHANNEL}\n\n"
                f"✂️ **Criado por:** {clip.get('creator_name', 'Desconhecido')}\n\n"
                f"👁️ **Visualizações:** {clip.get('view_count', 0)}\n\n"
                f"🕐 **Publicado:** {clip['created_at']}"
            ),
            "footer": {"text": "JOTTA Twitch Clip Watcher"},
        }],
    })


def get_twitch_clips():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("Configura TWITCH_CLIENT_ID e TWITCH_CLIENT_SECRET.")
    token = get_access_token()
    return get_last_24h_clips(token, get_channel_id(token))


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
        return
    successful = send_interval_message(cutoff, now, len(clips))
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


def main():
    state = load_state()
    try:
        if not DISCORD_WEBHOOK:
            raise RuntimeError("DISCORD_CLIP_WEBHOOK_URL não configurado.")
        process_twitch_clips(state)
    except Exception as error:
        # HTTP exceptions podem incluir URLs secretos; só expor erros controlados.
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f"Twitch: falhou: {detail}")
        raise
    finally:
        save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"CHECK FALHOU: {type(error).__name__}")
        sys.exit(1)
