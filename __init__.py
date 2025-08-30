import os
import re
from datetime import datetime
import requests
import paramiko
from ovos_workshop.skills.ovos import OVOSSkill
from ovos_bus_client.message import Message

class ObsidianAddNoteSkill(OVOSSkill):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Standaard pad naar settings.json in root van de skill
        settings_file = os.path.join(os.path.dirname(__file__), "settings.json")
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                self.settings = json.load(f)
        except Exception as e:
            self.log.warning(f"Kon settings.json niet laden: {e}")
            self.settings = {}

        # Instellingen
        self.vault_path = self.settings.get("obsidian_vault_path", "/tmp/obsidian")
        self.city = self.settings.get("city", "Amsterdam,nl")
        self.api_key = self.settings.get("openweathermap_api_key", None)

    def initialize(self):
        # Luister passief naar speak-events van ovos-persona
        self.bus.on("speak", self.handle_speak)

    def handle_speak(self, message: Message):
        meta = message.data.get("meta", {})
        # Alleen events van ovos-persona
        if meta.get("skill_id") != "ovos-persona":
            return

        utterance = message.data.get("utterance", "")
        match = self.note_pattern.search(utterance)
        if not match:
            return

        note_content = match.group(1).strip()
        parsed = self.parse_note_content(note_content)
        if not parsed:
            self.log.warning("Kon note content niet parsen")
            return

        title, goal, content = parsed
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        origin = "ovos"
        weather = self.get_weather()

        md_text = self.create_markdown(title, goal, content, timestamp, origin, weather)
        self.save_markdown(title, md_text)
        self.log.info(f"Notitie '{title}' aangemaakt in Obsidian vault")

    def parse_note_content(self, note_text):
        """Haalt Title, Goal en Content uit de LLM note output"""
        try:
            title_match = re.search(r"Title:\s*(.*)", note_text)
            goal_match = re.search(r"Goal:\s*(.*)", note_text)
            content_match = re.search(r"Content:\s*(.*)", note_text, re.DOTALL)
            if title_match and goal_match and content_match:
                return (title_match.group(1).strip(),
                        goal_match.group(1).strip(),
                        content_match.group(1).strip())  # <-- {content}
        except Exception as e:
            self.log.error(f"Error parsing note: {e}")
        return None

    def get_weather(self):
        """Haal korte weersomschrijving + temp op van OpenWeatherMap API"""
        if not self.api_key:
            self.log.warning("Geen OpenWeatherMap API key gevonden in settings.json")
            return "Onbekend"

        try:
            url = f"http://api.openweathermap.org/data/2.5/weather?q={self.city}&lang=nl&units=metric&appid={self.api_key}"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                desc = data["weather"][0]["description"]  # korte omschrijving
                temp = data["main"]["temp"]               # temperatuur
                return f"{desc}, {temp:.0f}°C"
            else:
                self.log.warning(f"Weer API gaf statuscode {response.status_code}")
        except Exception as e:
            self.log.warning(f"Weer ophalen mislukt: {e}")
        return "Onbekend"


    def create_markdown(self, title, goal, content, timestamp, origin, weather):
        """
        Maak de markdown notitie met de gewenste template:
        - Titel bovenaan
        - Categorie: Dagverslag
        - Dag, Week, Maand, Kwartaal, Jaar
        - ##Deze dag: Weer + Oorsprong
        - ##Inhoud: content van LLM
        """
        dt = datetime.now()
        weeknummer = dt.isocalendar()[1]
        dagnaam = dt.strftime("%A")          # bijv. Maandag, Dinsdag
        maandnaam = dt.strftime("%B")        # bijv. Januari, Februari
        kwartaal = (dt.month - 1) // 3 + 1
        jaar = dt.year

        template = f"""# {title}

    *Categorie:* Dagverslag  
    Dag: {dagnaam}  
    Week: W{weeknummer}  
    Maand: {maandnaam}  
    Kwartaal: Q{kwartaal}  
    Jaar: {jaar}  

    ## Deze dag:
    Weer: {weather}  
    Oorsprong: OVOS ObsidianAddNote Skill

    ## Inhoud
    {content}
    """
        return template


    def save_markdown(self, title, md_text):
        """
        Schrijf bestand naar een remote Windows (of Linux) systeem via SFTP (Paramiko).
        """
        ssh_cfg = (self.settings or {}).get("ssh", {})
        host = ssh_cfg.get("host")
        port = ssh_cfg.get("port", 22)
        username = ssh_cfg.get("username")
        password = ssh_cfg.get("password")
        remote_path = ssh_cfg.get("remote_path")

        if not (host and username and remote_path):
            self.log.error("SSH settings onvolledig: host/username/remote_path vereist")
            return

        # Veilige bestandsnaam
        safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "_", "-")).rstrip()
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_title}.md"
        remote_file = os.path.join(remote_path, filename)

        try:
            # Maak SSH client
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, port=port, username=username, password=password, timeout=10)

            # Start SFTP
            sftp = ssh.open_sftp()

            # Zorg dat remote pad bestaat (recursief)
            try:
                sftp.chdir(remote_path)
            except IOError:
                # Recursief mappen maken
                dirs = remote_path.strip("/").split("/")
                current = ""
                for d in dirs:
                    current += "/" + d
                    try:
                        sftp.chdir(current)
                    except IOError:
                        sftp.mkdir(current)
                        sftp.chdir(current)

            # Bestand schrijven
            with sftp.file(remote_file, "w", -1) as f:
                f.write(md_text)
            sftp.close()
            ssh.close()
            self.log.info(f"Notitie opgeslagen via SFTP: {remote_file}")

        except Exception as e:
            self.log.error(f"SFTP upload mislukt: {e}")