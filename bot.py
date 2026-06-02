import os
import asyncio
import secrets
import json
import time
import discord
from discord.ext import commands, tasks

import database as db
import vinted

TOKEN = os.environ["DISCORD_TOKEN"]

_domain = os.environ.get("REPLIT_DOMAINS", "").split(",")[0].strip()
AUTH_BASE_URL = f"https://{_domain}/api/auth/start" if _domain else "http://localhost:8080/api/auth/start"
AUTH_FILE = "/tmp/vinted_auth.json"

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Laufende Flows: discord_id -> {"step": ..., "data": {...}}
active_flows: dict[str, dict] = {}
# Offene Web-Auth-Links: code -> discord_id
pending_web_auths: dict[str, str] = {}


def _read_auth_file() -> dict:
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _clear_auth_code(code: str):
    try:
        data = _read_auth_file()
        data.pop(code, None)
        with open(AUTH_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


# User-IDs, die bereits über abgelaufenen Token benachrichtigt wurden (reset bei !anmelden)
_token_expired_notified: set[str] = set()

# ── Startup ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    db.init()
    sniper_loop.start()
    auth_poller.start()
    print(f"Eingeloggt als {bot.user} (ID: {bot.user.id})")
    print("Bot ist online!")


# ── Artikel-Buttons ───────────────────────────────────────────────────────────

class OfferModal(discord.ui.Modal, title="💬 Angebot senden"):
    price_input = discord.ui.TextInput(
        label="Dein Angebotspreis in €",
        placeholder="z.B. 15",
        min_length=1,
        max_length=10,
    )

    def __init__(self, item_id: str, discord_id: int, item_url: str):
        super().__init__()
        self.item_id = item_id
        self.discord_id = discord_id
        self.item_url = item_url

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            price = float(self.price_input.value.replace(",", ".").replace("€", "").strip())
        except ValueError:
            await interaction.followup.send("❌ Ungültiger Preis. Bitte nur eine Zahl eingeben.", ephemeral=True)
            return

        user_data = db.get_user(str(self.discord_id))
        if not user_data:
            await interaction.followup.send("❌ Du bist nicht angemeldet.", ephemeral=True)
            return

        success = await asyncio.to_thread(vinted.send_offer, user_data["cookies"], self.item_id, price)
        if success:
            await interaction.followup.send(f"✅ Angebot über **{price}€** gesendet!\n{self.item_url}", ephemeral=True)
        else:
            await interaction.followup.send(
                f"❌ Angebot konnte nicht gesendet werden.\nDirekt auf Vinted versuchen: {self.item_url}",
                ephemeral=True,
            )


CONDITION_MAP = {
    "new_with_tags": "Neu mit Etikett",
    "new_without_tags": "Neu ohne Etikett",
    "very_good": "Sehr gut",
    "good": "Gut",
    "satisfactory": "Befriedigend",
    6: "Neu mit Etikett",
    1: "Neu ohne Etikett",
    2: "Sehr gut",
    3: "Gut",
    4: "Befriedigend",
}


class ArticleView(discord.ui.View):
    def __init__(self, item_id: str, discord_id: int, item_url: str, seller_id: str | None = None):
        super().__init__(timeout=3600)
        self.item_id = item_id
        self.discord_id = discord_id
        self.item_url = item_url
        self.seller_id = seller_id
        self.add_item(discord.ui.Button(
            label="🔗 View",
            style=discord.ButtonStyle.link,
            url=item_url,
            row=1,
        ))

    def _check_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.discord_id

    @discord.ui.button(label="🚀 Autobuy", style=discord.ButtonStyle.success, row=0)
    async def autobuy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_owner(interaction):
            await interaction.response.send_message("❌ Das ist nicht deine Benachrichtigung.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        user_data = db.get_user(str(self.discord_id))
        if not user_data:
            await interaction.followup.send("❌ Nicht angemeldet. Nutze `!anmelden`.", ephemeral=True)
            return

        delivery_info = db.get_payment_info(str(self.discord_id))
        if not delivery_info:
            await interaction.followup.send(
                "⚠️ Keine Lieferadresse hinterlegt. Nutze `!adresse` um Hausadresse oder Abholpunkt einzurichten.",
                ephemeral=True,
            )
            return

        await interaction.followup.send("⏳ Versuche zu kaufen...", ephemeral=True)
        try:
            success, message = await asyncio.wait_for(
                asyncio.to_thread(
                    vinted.buy_item,
                    user_data["cookies"],
                    self.item_id,
                    self.item_url,
                    delivery_info,
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                f"⏱️ Zeitüberschreitung — Vinted hat nicht rechtzeitig geantwortet.\nDirekt kaufen: {self.item_url}",
                ephemeral=True,
            )
            return

        if success:
            button.disabled = True
            button.label = "✅ Gekauft!"
            await interaction.message.edit(view=self)
            await interaction.followup.send(f"✅ {message}", ephemeral=True)
        else:
            await interaction.followup.send(
                f"❌ Kauf fehlgeschlagen: {message}\nDirekt kaufen: {self.item_url}", ephemeral=True
            )

    @discord.ui.button(label="💸 Offer", style=discord.ButtonStyle.secondary, row=0)
    async def offer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_owner(interaction):
            await interaction.response.send_message("❌ Das ist nicht deine Benachrichtigung.", ephemeral=True)
            return
        modal = OfferModal(self.item_id, self.discord_id, self.item_url)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="❤️ Favourite", style=discord.ButtonStyle.danger, row=0)
    async def favorite_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_owner(interaction):
            await interaction.response.send_message("❌ Das ist nicht deine Benachrichtigung.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        user_data = db.get_user(str(self.discord_id))
        if not user_data:
            await interaction.followup.send("❌ Nicht angemeldet.", ephemeral=True)
            return

        success = await asyncio.to_thread(vinted.favorite, user_data["cookies"], self.item_id)
        if success:
            button.disabled = True
            button.label = "❤️ Gespeichert"
            await interaction.message.edit(view=self)
            await interaction.followup.send("❤️ Artikel favorisiert!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Konnte nicht favorisieren.", ephemeral=True)



# ── DM-Flow Handler ───────────────────────────────────────────────────────────

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    uid = str(message.author.id)

    if isinstance(message.channel, discord.DMChannel):
        raw = message.content.strip()

        # Erkennt ob die Nachricht wie ein Token aussieht:
        # - enthält "access_token_web=" ODER
        # - ist ein langer String der mit "eyJ" anfängt (JWT)
        looks_like_token = (
            "access_token_web=" in raw or
            "refresh_token_web=" in raw or
            (raw.startswith("eyJ") and len(raw) > 50) or
            (uid in active_flows and active_flows[uid].get("step") == "login_token")
        )

        if looks_like_token and not raw.startswith("!"):
            if uid in active_flows:
                del active_flows[uid]

            await message.channel.send("⏳ Überprüfe Token...")
            cookies = await asyncio.to_thread(vinted.login_with_token, raw)
            if not cookies:
                await message.channel.send(
                    "❌ Hat nicht geklappt.\n\n"
                    "Schicke beide Werte zusammen in diesem Format:\n"
                    "```\naccess_token_web=WERT1; refresh_token_web=WERT2\n```"
                    "Tippe `!anmelden` um die Anleitung nochmal zu sehen."
                )
                return

            if not cookies.get("refresh_token_web"):
                await message.channel.send(
                    "⚠️ Nur **access\\_token\\_web** erkannt — der **refresh\\_token\\_web** fehlt.\n\n"
                    "Bitte schicke **beide** zusammen:\n"
                    "```\naccess_token_web=WERT1; refresh_token_web=WERT2\n```"
                )
                return

            db.save_user(uid, "Vinted Nutzer", cookies)
            _token_expired_notified.discard(uid)
            await message.channel.send(
                "✅ **Anmeldung erfolgreich!**\n\n"
                "🔄 Der Bot erneuert deinen Zugang ab jetzt **automatisch** — du musst dich nie wieder anmelden.\n\n"
                "Nächste Schritte:\n"
                "• `!snipe Fred Perry 25` — Suche starten\n"
                "• `!adresse` — Lieferadresse hinterlegen (für Autobuy-Button)"
            )
            return

        # ── Adresse-Flow ──
        if uid in active_flows:
            flow = active_flows[uid]
            if flow["step"] == "addr_type":
                choice = message.content.strip()
                if choice == "1":
                    flow["data"]["delivery_type"] = "home"
                    flow["step"] = "addr_name"
                    await message.channel.send("👤 Vollständiger Name:")
                elif choice == "2":
                    flow["data"]["delivery_type"] = "pickup"
                    flow["step"] = "addr_name"
                    await message.channel.send("👤 Vollständiger Name:")
                else:
                    await message.channel.send("Bitte antworte mit **1** (Hausadresse) oder **2** (Abholpunkt).")
                return

            elif flow["step"] == "addr_name":
                flow["data"]["full_name"] = message.content.strip()
                if flow["data"].get("delivery_type") == "pickup":
                    flow["step"] = "addr_pickup_name"
                    await message.channel.send(
                        "📦 Name des Abholpunkts:\n"
                        "_(z.B. `DHL Packstation 109`, `Hermes Paketshop Musterstraße 5`)_"
                    )
                else:
                    flow["step"] = "addr_street"
                    await message.channel.send("🏠 Straße und Hausnummer:")
                return

            elif flow["step"] == "addr_pickup_name":
                flow["data"]["pickup_name"] = message.content.strip()
                flow["step"] = "addr_postal"
                await message.channel.send("📮 Postleitzahl:")
                return

            elif flow["step"] == "addr_street":
                flow["data"]["street"] = message.content.strip()
                flow["step"] = "addr_postal"
                await message.channel.send("📮 Postleitzahl:")
                return

            elif flow["step"] == "addr_postal":
                flow["data"]["postal_code"] = message.content.strip()
                flow["step"] = "addr_city"
                await message.channel.send("🏙️ Stadt:")
                return

            elif flow["step"] == "addr_city":
                flow["data"]["city"] = message.content.strip()
                d = flow["data"]
                del active_flows[uid]
                is_pickup = d.get("delivery_type") == "pickup"
                db.save_payment_info(
                    uid,
                    d.get("full_name", ""),
                    d.get("street", ""),
                    d.get("city", ""),
                    d.get("postal_code", ""),
                    delivery_type=d.get("delivery_type", "home"),
                    pickup_name=d.get("pickup_name"),
                )
                if is_pickup:
                    await message.channel.send(
                        f"✅ Abholpunkt gespeichert:\n"
                        f"**{d['full_name']}**\n"
                        f"📦 {d['pickup_name']}\n"
                        f"{d['postal_code']} {d['city']}\n\n"
                        f"Der 🛒 **Autobuy**-Button ist jetzt aktiv!"
                    )
                else:
                    await message.channel.send(
                        f"✅ Lieferadresse gespeichert:\n"
                        f"**{d['full_name']}**\n"
                        f"{d['street']}\n"
                        f"{d['postal_code']} {d['city']}\n\n"
                        f"Der 🛒 **Autobuy**-Button ist jetzt aktiv!"
                    )
                return

    await bot.process_commands(message)


# ── Befehle ───────────────────────────────────────────────────────────────────

@bot.command(name="anmelden")
async def anmelden(ctx):
    """Verbindet deinen Vinted-Account mit dem Bot."""
    uid = str(ctx.author.id)
    try:
        dm = await ctx.author.create_dm()
        active_flows[uid] = {"step": "login_token", "data": {}}
        await dm.send(
            "👋 **Vinted Sniper – Anmeldung** _(einmalig am Computer)_\n\n"
            "**Schritt 1:** Öffne **www.vinted.de** und logge dich ein\n\n"
            "**Schritt 2:** Öffne die Entwicklertools:\n"
            "• Windows/Linux: **F12** drücken\n"
            "• Mac: **Cmd + Option + I** drücken (⌘ + ⌥ + I)\n\n"
            "**Schritt 3:** Klicke oben auf **'Application'** _(Chrome)_ oder **'Speicher'** _(Firefox)_\n\n"
            "**Schritt 4:** Links im Menü: **Cookies** → **www.vinted.de** anklicken\n\n"
            "**Schritt 5:** Kopiere nacheinander die Werte aus diesen zwei Zeilen:\n"
            "• **access\\_token\\_web** → Wert kopieren\n"
            "• **refresh\\_token\\_web** → Wert kopieren\n\n"
            "**Schritt 6:** Schicke **beide Werte zusammen** in diesem Format hier ⬇️\n\n"
            "```\naccess_token_web=WERT1; refresh_token_web=WERT2\n```"
        )
        if not isinstance(ctx.channel, discord.DMChannel):
            await ctx.send(f"{ctx.author.mention} Ich habe dir eine DM geschickt — schau rein! 📬")
    except discord.Forbidden:
        await ctx.send("❌ Ich kann dir keine DM schicken. Bitte erlaube Direktnachrichten von Server-Mitgliedern.")


@bot.command(name="adresse")
async def adresse(ctx):
    """Lieferadresse für den Autobuy-Button hinterlegen."""
    uid = str(ctx.author.id)
    if not db.get_user(uid):
        await ctx.send("Du bist nicht angemeldet. Nutze zuerst `!anmelden`.")
        return
    try:
        dm = await ctx.author.create_dm()
        active_flows[uid] = {"step": "addr_type", "data": {}}
        await dm.send(
            "📦 **Lieferung einrichten**\n\n"
            "Wie soll das Paket geliefert werden?\n\n"
            "**1️⃣** — Hausadresse\n"
            "**2️⃣** — Abholpunkt (Packstation / Paketshop)\n\n"
            "Tippe **1** oder **2**:"
        )
        if not isinstance(ctx.channel, discord.DMChannel):
            await ctx.send(f"{ctx.author.mention} Ich habe dir eine DM geschickt! 📬")
    except discord.Forbidden:
        await ctx.send("❌ Ich kann dir keine DM schicken.")


@bot.command(name="meinadresse")
async def meinadresse(ctx):
    """Zeigt deine gespeicherte Lieferadresse."""
    uid = str(ctx.author.id)
    info = db.get_payment_info(uid)
    if not info:
        await ctx.send("Du hast noch keine Adresse hinterlegt. Nutze `!adresse`.")
        return
    is_pickup = info.get("delivery_type") == "pickup"
    if is_pickup:
        embed = discord.Embed(title="📦 Dein Abholpunkt", color=discord.Color.green())
        embed.add_field(name="Name", value=info["full_name"], inline=False)
        embed.add_field(name="Abholpunkt", value=info.get("pickup_name") or "—", inline=False)
        embed.add_field(name="PLZ / Stadt", value=f"{info['postal_code']} {info['city']}", inline=False)
    else:
        embed = discord.Embed(title="🏠 Deine Lieferadresse", color=discord.Color.green())
        embed.add_field(name="Name", value=info["full_name"], inline=False)
        embed.add_field(name="Adresse", value=f"{info['street']}\n{info['postal_code']} {info['city']}", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="abmelden")
async def abmelden(ctx):
    """Trennt deinen Vinted-Account und löscht alle Daten."""
    uid = str(ctx.author.id)
    if not db.get_user(uid):
        await ctx.send("Du bist nicht angemeldet.")
        return
    db.delete_user(uid)
    await ctx.send("✅ Dein Account und alle Daten wurden gelöscht.")


def _matches_keyword(title: str, keyword: str) -> bool:
    """
    Prüft ob alle Wörter des Suchbegriffs im Artikeltitel vorkommen (Reihenfolge egal).
    Ignoriert Groß-/Kleinschreibung und Sonderzeichen.
    Beispiel: keyword='Fred Perry' → title muss 'fred' UND 'perry' enthalten.
    """
    import re
    title_clean = re.sub(r"[^a-z0-9äöüß]", " ", title.lower())
    for word in keyword.lower().split():
        word_clean = re.sub(r"[^a-z0-9äöüß]", "", word)
        if word_clean and word_clean not in title_clean:
            return False
    return True


def _iso_to_flag(iso: str) -> str:
    """Konvertiert einen ISO-3166-1-Alpha-2-Code in ein Flaggen-Emoji. z.B. 'DE' → '🇩🇪'"""
    try:
        iso = iso.strip().upper()
        if len(iso) != 2:
            return ""
        return chr(0x1F1E6 + ord(iso[0]) - ord("A")) + chr(0x1F1E6 + ord(iso[1]) - ord("A"))
    except Exception:
        return ""


def _channel_name(keyword: str, max_price: float) -> str:
    """Erstellt einen gültigen Discord-Kanalnamen aus Suchbegriff und Preis."""
    import re
    name = keyword.lower()
    name = re.sub(r"[^a-z0-9äöüß\s-]", "", name)
    name = re.sub(r"\s+", "-", name.strip())
    name = f"{name}-bis-{int(max_price)}€"
    return name[:100]


async def _get_or_create_category(guild: discord.Guild) -> discord.CategoryChannel:
    """Gibt die 'Vinted Sniper' Kategorie zurück oder erstellt sie."""
    for cat in guild.categories:
        if cat.name == "🏇 Vinted Sniper":
            return cat
    return await guild.create_category("🏇 Vinted Sniper")


@bot.command(name="snipe")
async def snipe(ctx, *args):
    """Neuen Suchauftrag hinzufügen. Beispiel: !snipe Fred Perry 25"""
    uid = str(ctx.author.id)
    if not db.get_user(uid):
        await ctx.send("Du bist nicht angemeldet. Nutze zuerst `!anmelden`.")
        return
    if len(args) < 2:
        await ctx.send("Bitte gib Suchbegriff und Maximalpreis an.\nBeispiel: `!snipe Fred Perry 25`")
        return
    try:
        max_price = float(args[-1])
    except ValueError:
        await ctx.send("❌ Der letzte Wert muss ein Preis sein. Beispiel: `!snipe Nike Schuhe 30`")
        return
    keyword = " ".join(args[:-1])

    # Kanal erstellen falls im Server-Kontext
    channel_id = None
    guild_id = None
    if ctx.guild:
        try:
            guild_id = str(ctx.guild.id)
            category = await _get_or_create_category(ctx.guild)
            ch_name = _channel_name(keyword, max_price)
            # Prüfen ob Kanal schon existiert
            existing = discord.utils.get(ctx.guild.text_channels, name=ch_name)
            if existing:
                channel = existing
            else:
                channel = await ctx.guild.create_text_channel(
                    ch_name,
                    category=category,
                    topic=f"Vinted Sniper · {keyword} · max. {max_price}€"
                )
            channel_id = str(channel.id)
            await ctx.send(
                f"✅ Suchauftrag gestartet: **{keyword}** bis **{max_price}€**\n"
                f"Neue Artikel erscheinen in {channel.mention} 🔔"
            )
        except discord.Forbidden:
            await ctx.send(
                f"✅ Suchauftrag gestartet: **{keyword}** bis **{max_price}€**\n"
                f"⚠️ Ich habe keine Berechtigung Kanäle zu erstellen — Benachrichtigungen kommen per DM."
            )
    else:
        await ctx.send(
            f"✅ Suchauftrag: **{keyword}** bis **{max_price}€**\n"
            f"Benachrichtigungen kommen per DM. Tippe den Befehl im Server um einen eigenen Kanal zu bekommen."
        )

    db.add_search(uid, keyword, max_price, guild_id=guild_id, channel_id=channel_id)


@bot.command(name="stopp")
async def stopp(ctx, *, keyword: str = None):
    """Suchauftrag stoppen. Beispiel: !stopp Fred Perry"""
    uid = str(ctx.author.id)
    if not keyword:
        await ctx.send("Bitte gib den Suchbegriff an. Beispiel: `!stopp Fred Perry`")
        return

    # Kanal löschen falls vorhanden
    searches = db.get_searches(uid)
    for s in searches:
        if s["keyword"].lower() == keyword.lower() and s.get("channel_id"):
            ch = bot.get_channel(int(s["channel_id"]))
            if ch:
                try:
                    await ch.delete(reason=f"Suchauftrag '{keyword}' gestoppt")
                except discord.Forbidden:
                    pass

    db.remove_search(uid, keyword)
    await ctx.send(f"✅ Suchauftrag **{keyword}** entfernt.")


@bot.command(name="suchen")
async def suchen(ctx):
    """Zeigt alle deine aktiven Suchaufträge."""
    uid = str(ctx.author.id)
    if not db.get_user(uid):
        await ctx.send("Du bist nicht angemeldet. Nutze `!anmelden`.")
        return
    searches = db.get_searches(uid)
    if not searches:
        await ctx.send("Keine aktiven Suchaufträge.\nHinzufügen mit: `!snipe [Begriff] [Maxpreis]`")
        return
    embed = discord.Embed(title="🔍 Deine Suchaufträge", color=discord.Color.green())
    for s in searches:
        embed.add_field(name=s["keyword"], value=f"bis **{s['max_price']}€**", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="hilfe")
async def hilfe(ctx):
    """Zeigt alle Befehle."""
    embed = discord.Embed(title="📋 Vinted Sniper – Befehle", color=discord.Color.blurple())
    embed.add_field(name="!anmelden", value="Vinted-Account verbinden (per DM)", inline=False)
    embed.add_field(name="!adresse", value="Lieferadresse hinterlegen (für Autobuy-Button)", inline=False)
    embed.add_field(name="!meinadresse", value="Gespeicherte Adresse anzeigen", inline=False)
    embed.add_field(name="!snipe [Begriff] [Preis]", value="Suchauftrag starten\nz.B. `!snipe Fred Perry 25`", inline=False)
    embed.add_field(name="!stopp [Begriff]", value="Suchauftrag stoppen", inline=False)
    embed.add_field(name="!suchen", value="Aktive Suchaufträge anzeigen", inline=False)
    embed.add_field(name="!abmelden", value="Account & alle Daten löschen", inline=False)
    embed.add_field(name="!ping", value="Bot-Latenz prüfen", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"Pong! 🏓 Latenz: {round(bot.latency * 1000)}ms")


# ── Auth-Poller (prüft ob jemand die Webseite ausgefüllt hat) ─────────────────

@tasks.loop(seconds=5)
async def auth_poller():
    if not pending_web_auths:
        return
    data = _read_auth_file()
    for code, uid in list(pending_web_auths.items()):
        if code not in data:
            continue
        raw_token = data[code]["token"]
        del pending_web_auths[code]
        _clear_auth_code(code)

        cookies = await asyncio.to_thread(vinted.login_with_token, raw_token)
        try:
            user = await bot.fetch_user(int(uid))
            dm = await user.create_dm()
            if not cookies:
                await dm.send(
                    "❌ Token konnte nicht bestätigt werden.\n"
                    "Bitte stelle sicher dass du auf **www.vinted.de eingeloggt** bist "
                    "und versuche `!anmelden` erneut."
                )
                continue
            username = await asyncio.to_thread(vinted.get_username, cookies)
            db.save_user(uid, username or "Unbekannt", cookies)
            _token_expired_notified.discard(uid)
            await dm.send(
                f"✅ Eingeloggt als **{username}**!\n\n"
                f"Nächste Schritte:\n"
                f"• `!snipe Fred Perry 25` — Suche starten\n"
                f"• `!adresse` — Lieferadresse hinterlegen (für Autobuy)"
            )
        except Exception as e:
            print(f"[Auth-Poller] Fehler für uid {uid}: {e}")


@auth_poller.before_loop
async def before_auth_poller():
    await bot.wait_until_ready()


# ── Hintergrund-Sniping-Loop ──────────────────────────────────────────────────

@tasks.loop(seconds=60)
async def sniper_loop():
    searches = db.get_all_searches()
    if not searches:
        return

    print(f"[Sniper] Prüfe {len(searches)} Suchauftrag/Aufträge...")

    for search in searches:
        uid = search["discord_id"]
        user_data = db.get_user(uid)
        if not user_data:
            continue

        cookies = user_data["cookies"]
        keyword = search["keyword"]
        max_price = search["max_price"]

        try:
            items, token_expired = await asyncio.to_thread(vinted.search, cookies, keyword, max_price)
        except Exception as e:
            print(f"[Sniper] Suche-Fehler '{keyword}': {e}")
            continue

        if token_expired:
            print(f"[Sniper] Token abgelaufen für {uid}, versuche Refresh...")
            new_cookies = await asyncio.to_thread(vinted.refresh_access_token, cookies)
            if new_cookies:
                db.save_user(uid, user_data["vinted_user"], new_cookies)
                cookies = new_cookies
                print(f"[Sniper] Token erfolgreich erneuert für {uid}")
                items, _ = await asyncio.to_thread(vinted.search, cookies, keyword, max_price)
            else:
                print(f"[Sniper] Refresh fehlgeschlagen für {uid}, benachrichtige Nutzer")
                if uid not in _token_expired_notified:
                    _token_expired_notified.add(uid)
                    try:
                        discord_user = await bot.fetch_user(int(uid))
                        await discord_user.send(
                            "⚠️ **Vinted Sniper – Anmeldung abgelaufen**\n\n"
                            "Dein Vinted-Token ist abgelaufen und konnte leider nicht automatisch erneuert werden.\n"
                            "Bitte melde dich einmalig neu an:\n\n"
                            "👉 Tippe `!anmelden` (nur am Computer nötig)"
                        )
                    except Exception:
                        pass
                continue

        for item in items:
            item_id = str(item.get("id", ""))
            if not item_id or db.is_seen(item_id, uid):
                continue

            title = item.get("title", "Unbekannt")

            # Exakter Titelfilter: alle Suchwörter müssen im Titel vorkommen
            if not _matches_keyword(title, keyword):
                db.mark_seen(item_id, uid)  # trotzdem als gesehen markieren
                print(f"[Sniper] ⏭️ Übersprungen (kein Match): {title!r}")
                continue

            db.mark_seen(item_id, uid)
            price_raw = item.get("price", {}).get("amount", "?")
            url = item.get("url", "")
            full_url = f"https://www.vinted.de{url}" if url.startswith("/") else url
            image_url = item.get("photo", {}).get("url", "")

            # Verkäufer-Details nachladen (Katalog liefert nur id/login)
            seller_user = item.get("user", {})
            seller_id = seller_user.get("id")
            seller_login = seller_user.get("login", "Unbekannt")
            seller_profile = seller_user.get("profile_url", "")
            seller_avatar = seller_user.get("photo", {}).get("url", "") if isinstance(seller_user.get("photo"), dict) else ""

            # Blockierten Verkäufer überspringen
            if seller_id and db.is_blocked(uid, str(seller_id)):
                print(f"[Sniper] 🚫 Blockierter Verkäufer übersprungen: {seller_login}")
                continue

            seller_info = None
            if seller_id:
                seller_info = await asyncio.to_thread(vinted.get_user_info, cookies, seller_id)

            # Bewertung & Standort aus dem vollen Nutzerprofil
            flag = ""
            seller_location = "k.A."
            seller_rating_str = "k.A."
            if seller_info:
                rep = seller_info.get("feedback_reputation")
                pos_count = seller_info.get("positive_feedback_count") or seller_info.get("feedback_count") or 0
                if rep is not None:
                    try:
                        rep_f = float(rep)
                        stars_float = rep_f if rep_f > 5 else rep_f * 5
                        filled = max(0, min(5, int(round(stars_float))))
                        star_str = "⭐" * filled + "☆" * (5 - filled)
                        seller_rating_str = f"{star_str} ({pos_count})"
                    except Exception:
                        pass

                city = seller_info.get("city") or ""
                country = seller_info.get("country_title") or seller_info.get("country", {})
                if isinstance(country, dict):
                    country = country.get("title", "")
                iso = seller_info.get("country_iso_code") or seller_info.get("country", {})
                if isinstance(iso, dict):
                    iso = iso.get("iso_code", "")
                flag = _iso_to_flag(str(iso)) if iso else ""
                location_parts = ", ".join(filter(None, [str(city), str(country)]))
                seller_location = f"{flag} {location_parts}".strip() if location_parts else "k.A."

            # Käuferschutz & Gesamtpreis
            try:
                fee_amount = item.get("service_fee", {}).get("amount")
                total_amount = item.get("total_item_price", {}).get("amount")
                if fee_amount is not None:
                    kaeufer_fee = float(fee_amount)
                    total_with_fee = float(total_amount) if total_amount else round(float(price_raw) + kaeufer_fee, 2)
                else:
                    p = float(price_raw)
                    kaeufer_fee = round(max(0.70, p * 0.05), 2)
                    total_with_fee = round(p + kaeufer_fee, 2)
            except Exception:
                kaeufer_fee = 0.0
                total_with_fee = 0.0

            # Brand & Größe aus dem Katalog
            brand = item.get("brand_title") or "—"
            size = item.get("size_title") or "—"

            # Zustand direkt aus dem Katalog (status ist bereits auf Deutsch)
            condition = item.get("status") or "—"
            posted_ts = int(time.time())
            posted_str = f"<t:{posted_ts}:R>"  # Discord zeigt "vor X Sek./Min." relativ

            # Auto-Favorit
            await asyncio.to_thread(vinted.favorite, cookies, item_id)

            # Fotos sammeln (bis zu 3)
            photos = item.get("photos", [])
            photo_urls = []
            for ph in photos[:3]:
                u = ph.get("url") or ph.get("full_size_url", "")
                if u:
                    photo_urls.append(u)
            if not photo_urls and image_url:
                photo_urls = [image_url]

            # ── Embed im Archiev-Stil ──────────────────────────────────────────
            title_line = f"{flag} {title} | {price_raw} €" if flag else f"{title} | {price_raw} €"
            embed = discord.Embed(
                title=title_line,
                url=full_url,
                color=discord.Color.from_rgb(249, 168, 37),
            )
            embed.set_author(
                name=seller_login,
                url=seller_profile or None,
                icon_url=seller_avatar or None,
            )

            # Zeile 1: Preis | Marke | Größe
            embed.add_field(name="💰 Price", value=f"{price_raw} € (+ {kaeufer_fee:.2f} €)", inline=True)
            embed.add_field(name="🏷️ Brand", value=brand, inline=True)
            embed.add_field(name="✂️ Size", value=size, inline=True)

            # Zeile 2: Erhalten | Bewertung | Zustand
            embed.add_field(name="⏱️ Erhalten", value=posted_str, inline=True)
            embed.add_field(name="⭐ Reviews", value=seller_rating_str, inline=True)
            embed.add_field(name="✨ Condition", value=condition, inline=True)

            if photo_urls:
                embed.set_image(url=photo_urls[0])
            embed.set_footer(text=f"🔍 {keyword} • Vinted Sniper")

            # Zusatz-Embeds für Foto 2 und 3 (gleiche URL → Discord zeigt Galerie)
            extra_embeds = []
            for extra_url in photo_urls[1:3]:
                e = discord.Embed(url=full_url, color=discord.Color.orange())
                e.set_image(url=extra_url)
                extra_embeds.append(e)

            all_embeds = [embed] + extra_embeds

            # Artikel-Buttons View
            view = ArticleView(item_id, int(uid), full_url, seller_id=str(seller_id) if seller_id else None)

            # In Kanal posten (falls vorhanden), sonst per DM
            channel_id = search.get("channel_id")
            sent = False
            if channel_id:
                ch = bot.get_channel(int(channel_id))
                if ch:
                    try:
                        await ch.send(embeds=all_embeds, view=view)
                        print(f"[Sniper] ✅ #{ch.name}: {title} ({price_raw}€) [{len(all_embeds)} Foto(s)]")
                        sent = True
                    except Exception as e:
                        print(f"[Sniper] Kanal-Fehler: {e}")
            if not sent:
                try:
                    discord_user = await bot.fetch_user(int(uid))
                    await discord_user.send(embeds=all_embeds, view=view)
                    print(f"[Sniper] ✅ DM → {discord_user.name}: {title} ({price_raw}€) [{len(all_embeds)} Foto(s)]")
                except Exception as e:
                    print(f"[Sniper] DM-Fehler für {uid}: {e}")

        await asyncio.sleep(2)


@sniper_loop.before_loop
async def before_sniper():
    await bot.wait_until_ready()


# ── Start ─────────────────────────────────────────────────────────────────────

bot.run(TOKEN)
