import os
import re
from datetime import datetime
import requests
import paramiko
import json
from ovos_workshop.skills.ovos import OVOSSkill
from ovos_bus_client.message import Message

class ObsidianAddNoteSkill(OVOSSkill):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.log = logging.getLogger(__name__)
        # Regex om NOTE te detecteren in LLM output
        self.note_pattern = re.compile(r"\bNOTE\b(.*)", re.DOTALL)

    def initialize(self):
        # Subscribe to all speak events
        self.add_event("ovos.speech.recognition.intent_response", self.handle_speak)
        self.add_event("speak", self.handle_speak)

    def handle_speak(self, message: Message):
        # Huidige utterance van de LLM
        utterance = message.data.get("utterance", "")
        if not utterance:
            return

        meta = message.data.get("meta", {})
        skill_source = meta.get("skill_id") or meta.get("skill")
        if skill_source != "persona.openvoiceos":
            return  # Alleen events van persona

        # Zoek naar NOTE
        match = self.note_pattern.search(utterance)
        if not match:
            return

        note_block = match.group(1)
        title = self._extract_field(note_block, "Titel:")
        goal = self._extract_field(note_block, "Doel:")
        content = self._extract_field(note_block, "Inhoud:")

        # Maak notitie aan
        self.add_note(title, goal, content)
    
    def _extract_field(self, text, label):
        pattern = rf"{label}\s*(.*)"
        m = re.search(pattern, text)
        return m.group(1).strip() if m else ""

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


    def add_note(self, title, goal, content):
        # Haal SSH/remote instellingen uit self.settings
        ssh_cfg = self.settings.get("ssh", {})
        host = ssh_cfg.get("host")
        port = ssh_cfg.get("port", 22)
        username = ssh_cfg.get("username")
        password = ssh_cfg.get("password")
        remote_path = ssh_cfg.get("remote_path")

        if not (host and username and remote_path):
            self.log.error("SSH settings incompleet")
            return

        # Weer ophalen
        weather = self.get_weather()

        # Markdown maken
        timestamp = datetime.now()
        markdown_text = self.create_markdown(title, goal, content, timestamp, "OVOS ObsidianAddNote Skill", weather)

        # Filename veilig maken
        filename_safe = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{title.replace(' ', '_')}.md"
        remote_file = os.path.join(remote_path, filename_safe)

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, port=port, username=username, password=password, timeout=10)
            sftp = ssh.open_sftp()

            # Maak remote folder aan indien nodig
            try:
                sftp.chdir(remote_path)
            except IOError:
                dirs = remote_path.strip("/").split("/")
                current = ""
                for d in dirs:
                    current += "/" + d
                    try:
                        sftp.chdir(current)
                    except IOError:
                        sftp.mkdir(current)
                        sftp.chdir(current)

            # Schrijf bestand
            with sftp.file(remote_file, "w", -1) as f:
                f.write(markdown_text)

            sftp.close()
            ssh.close()
            self.log.info(f"Notitie opgeslagen via SFTP: {remote_file}")

        except Exception as e:
            self.log.error(f"SFTP upload mislukt: {e}")
