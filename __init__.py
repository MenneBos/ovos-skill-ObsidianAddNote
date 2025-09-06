import re
import logging
from datetime import datetime
import os
import requests
import paramiko
from ovos_workshop.skills.ovos import OVOSSkill
from ovos_bus_client.message import Message
from ovos_utils.log import LOG

DEFAULT_SETTINGS = {
    "log_level": "INFO"
}

class ObsidianAddNoteSkill(OVOSSkill):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Regex om NOTE te detecteren in LLM output
        #self.note_pattern = re.compile(r"\bNOTE\b(.*)", re.DOTALL)
        #self.note_pattern = re.compile(r"\[?NOTE\]?(.*)", re.DOTALL)
        self.note_start_pattern = re.compile(r"\[?\s*NOTE\s*\]?", re.IGNORECASE)
        self.note_end_pattern = re.compile(r"\[?\s*ENDNOTE\s*\]?", re.IGNORECASE)
        # API / settings placeholders
        self.api_key = None
        self.city = None

    def initialize(self):
        self.collecting_note = False
        self.current_note = {"title": None, "goal": None, "content": ""}
        self.await_field = None
        # Haal settings uit OVOS settings
        self.api_key = self.settings.get("api_key")
        self.city = self.settings.get("city", "Nederland")
        # Subscribe naar speak events
        self.add_event("speak", self.handle_speak)
        self.add_event("ovos.speech.recognition.intent_response", self.handle_speak)
        LOG.info("ObsidianAddNoteSkill ready")

    def handle_speak(self, message):
        utterance = message.data.get("utterance", "").strip()
        meta = message.data.get("meta", {})
        skill_source = meta.get("skill_id") or meta.get("skill")
        if skill_source != "persona.openvoiceos":
            return

        # Start nieuwe note
        if self.note_start_pattern.search(utterance):
            self.collecting_note = True
            self.current_note = {"title": None, "goal": None, "content": ""}
            self.await_field = None
            self.log.info("NOTE detected, start collecting note")
            return

        if not self.collecting_note:
            return

        # Detecteer ENDNOTE
        if self.note_end_pattern.search(utterance):
            self.log.info(f"ENDNOTE detected, final note: {self.current_note}")
            self.add_note(
                self.current_note["title"],
                self.current_note["goal"],
                self.current_note["content"].strip()
            )
            self.collecting_note = False
            self.current_note = {"title": None, "goal": None, "content": ""}
            self.await_field = None
            return
        
        # Collect fields
        if self.await_field:
            # Append to content if collecting content, otherwise just set
            if self.await_field == "content":
                self.current_note["content"] += utterance + "\n"
            else:
                self.current_note[self.await_field] = utterance
            self.await_field = None
            return
        
        # Detect which field comes next
        if utterance.lower().startswith("titel:"):
            self.await_field = "title"
        elif utterance.lower().startswith("doel:"):
            self.await_field = "goal"
        elif utterance.lower().startswith("inhoud:"):
            self.await_field = "content"

        # Debug log
        self.log.debug(f"Collecting note: {self.current_note}, awaiting field: {self.await_field}")


    def _extract_field(self, text, label):
        pattern = rf"{label}\s*(.*)"
        m = re.search(pattern, text)
        return m.group(1).strip() if m else ""

    def get_weather(self):
        """Haal korte weersomschrijving + temp op van OpenWeatherMap API"""
        if not self.api_key:
            LOG.debug("Geen OpenWeatherMap API key gevonden in settings")
            return "Onbekend"
        try:
            url = f"http://api.openweathermap.org/data/2.5/weather?q={self.city}&lang=nl&units=metric&appid={self.api_key}"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                desc = data["weather"][0]["description"]
                temp = data["main"]["temp"]
                return f"{desc}, {temp:.0f}°C"
            else:
                LOG.warning(f"Weer API gaf statuscode {response.status_code}")
        except Exception as e:
            LOG.warning(f"Weer ophalen mislukt: {e}")
        return "Onbekend"

    def create_markdown(self, title, goal, content, timestamp, origin, weather):
        """Maak de markdown notitie met jouw template"""
        weeknummer = timestamp.isocalendar()[1]
        dagnaam = timestamp.strftime("%A")
        maandnaam = timestamp.strftime("%B")
        kwartaal = (timestamp.month - 1) // 3 + 1
        jaar = timestamp.year

        template = f"""# {title}

*Categorie:* Dagverslag  
Dag: {dagnaam}  
Week: W{weeknummer}  
Maand: {maandnaam}  
Kwartaal: Q{kwartaal}  
Jaar: {jaar}  

## Deze dag:
Weer: {weather}  
Oorsprong: {origin}

## Inhoud
{content}
"""
        return template

    def add_note(self, title, goal, content):
        """Upload markdown via Paramiko SFTP"""
        ssh_cfg = self.settings.get("ssh", {})
        host = ssh_cfg.get("host")
        port = ssh_cfg.get("port", 22)
        username = ssh_cfg.get("username")
        password = ssh_cfg.get("password")
        remote_path = ssh_cfg.get("remote_path")

        if not (host and username and remote_path):
            LOG.error("SSH settings incompleet")
            return
        
        if not all([title, goal, content]):
            self.log.warning("Cannot add note, missing fields")
            return
        self.log.info(f"Adding note:\nTitle: {title}\nGoal: {goal}\nContent:\n{content}")

        weather = self.get_weather()
        timestamp = datetime.now()
        markdown_text = self.create_markdown(title, goal, content, timestamp, "OVOS ObsidianAddNote Skill", weather)
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

            with sftp.file(remote_file, "w", -1) as f:
                f.write(markdown_text)

            sftp.close()
            ssh.close()
            self.log.info(f"Notitie opgeslagen via SFTP: {remote_file}")

        except Exception as e:
            self.log.error(f"SFTP upload mislukt: {e}")
