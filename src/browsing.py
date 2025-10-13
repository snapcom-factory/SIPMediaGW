#!/usr/bin/env python
"""
-------------------------------------------------------------
FONCTIONNEMENT GÉNÉRAL (browsing.py)
-------------------------------------------------------------
Classe générique “Browsing” qui orchestre :
- le chargement de la page (loadPage, surchargée par Teams),
- l’injection du JS front (loadJS + join),
- la boucle d’interaction (interact + chatHandler),
- l’arrêt propre (unset + stop),
- des utilitaires (monitorSingleParticipant).

Le JS injecté expose `window.Browsing` (alias de Teams) et
on crée `window.meeting = new window.Browsing(...)` puis
on appelle `window.meeting.join()`.
-------------------------------------------------------------
"""
import sys
import os
import traceback
import queue
import base64
import time
import threading
import subprocess

class Browsing:
    def __init__(self, width, height, config,
                 modName=None, room=None, name=None,
                 driver=None, inputs=None):
        """
        Initialise l’environnement d’automatisation.
        - width/height : dimensions souhaitées (info, non utilisées ici)
        - config : configuration globale
        - modName : nom du module JS à charger (ex. 'teams')
        - room : infos de la salle (domain, roomName, displayName, lang, token)
        - driver : instance Selenium WebDriver
        - inputs : queue d’entrées utilisateur (touches, etc.)
        """
        self.url = ''
        self.room = room if room else {}
        self.name = name if name else ''
        self.width = width
        self.height = height
        self.config = config
        self.modName =  modName
        self.initScript = ''
        self.screenShared = False
        self.userInputs = inputs
        self.driver = driver
        self.chatMsg = queue.Queue()

    def loadJS(self, jsScript):
        """
        Lit un fichier JS et l’exécute *directement* avec execute_script.
        - Avantage : évite certains blocages CSP/TrustedTypes liés aux <script>.
        - Ajoute un commentaire //# sourceURL=... pour nommer le script dans DevTools.
        """
        with open(jsScript, "r", encoding="utf-8") as f:
            js_code = f.read()
        if "sourceURL=" not in js_code:
            js_code += "\n//# sourceURL=" + os.path.basename(jsScript)
        self.driver.execute_script(js_code)

    def loadImages(self, path, lang):
        """
        Charge des images (optionnel, pour un menu IVR par exemple) en Base64.
        """
        with open(path + "icon.png", "rb") as f:
            self.iconB64 = "data:image/png;base64,{}".format(base64.b64encode(f.read()).decode("utf-8"))
        with open(path + "dtmf_{}.png".format(lang), "rb") as f:
            self.dtmfB64 = "data:image/png;base64,{}".format(base64.b64encode(f.read()).decode("utf-8"))

    def loadPage(self):
        """
        À surcharger par la sous-classe (ex. Teams) pour ouvrir la bonne page
        et effectuer des pré-requis (switch iframe, etc.).
        """
        pass

    def join(self):
        """
        Injecte le module JS (assets/{modName}.js), crée `window.meeting`
        (instance de window.Browsing = Teams), puis appelle `meeting.join()`.
        Installe aussi un “watcher” pour récréer meeting si la SPA se réinitialise.
        """
        # 1) Charger le module front (par ex. teams.js)
        self.loadJS(os.path.join(os.path.dirname(os.path.normpath(__file__)),
                                 '../browsing/assets/{}.js'.format(self.modName)))

        # 2) Instancier l’objet meeting côté page
        self.initScript = (
            "window.meeting = new window.Browsing("
            "'{domain}', '{room}', '{display}', '{lang}', '{token}'"
            ");"
        ).format(
            domain=self.room['config']['webrtc_domain'],
            room=self.room['roomName'],
            display=self.room['displayName'],
            lang=self.room['config']['lang'],
            token=self.room['roomToken']
        )
        self.driver.execute_script(self.initScript)

        # 3) Lancer le pré-join
        self.driver.execute_script("window.meeting.join();")

        # 4) (Optionnel) Watcher simple pour réinitialiser l’objet sur navigation SPA
        self.driver.execute_script("""
          (function(domain, room, display, lang, token){
            if (window.__meetingWatcherInstalled) return;
            window.__meetingWatcherInstalled = true;
            const reinit = () => {
              try {
                if (!window.meeting && window.Browsing) {
                  window.meeting = new window.Browsing(domain, room, display, lang, token);
                  console.log('[Watcher] meeting ré-initialisé');
                }
              } catch(e) { console.warn('[Watcher] réinit a échoué', e); }
            };
            ['hashchange','popstate','pageshow'].forEach(ev => window.addEventListener(ev, reinit));
          })(arguments[0], arguments[1], arguments[2], arguments[3], arguments[4]);
        """, self.room['config']['webrtc_domain'],
             self.room['roomName'],
             self.room['displayName'],
             self.room['config']['lang'],
             self.room['roomToken'])

    def monitorSingleParticipant(self, thresholdSeconds=300, checkInterval=60):
        """
        Surveille le nombre de participants via window.meeting.getParticipantNum().
        Si on reste seul pendant `thresholdSeconds`, exécute une commande (ex: /quit).
        - checkInterval : période de vérification en secondes.
        """
        singleStartTime = None

        def checkLoop():
            nonlocal singleStartTime
            while self.driver.execute_script("return('getParticipantNum' in window.meeting)"):
                try:
                    participantNum = self.driver.execute_script(
                        "return (window.meeting && window.meeting.getParticipantNum) ? window.meeting.getParticipantNum() : 1;"
                    )
                except Exception as e:
                    print(f"Error getting participant number: {e}", flush=True)
                    participantNum = None

                if participantNum  and participantNum <= 1:
                    if singleStartTime is None:
                        singleStartTime = time.time()
                    elif time.time() - singleStartTime >= thresholdSeconds:
                        print(f"Single participant detected for {thresholdSeconds} seconds.", flush=True)
                        subprocess.run(['echo "/quit" | netcat -q 1 127.0.0.1 5555'], shell=True)
                        break
                else:
                    singleStartTime = None

                time.sleep(checkInterval)

        thread = threading.Thread(target=checkLoop, daemon=True)
        thread.start()
        return thread

    def browse(self):
        """Point d’extension pour une navigation spécifique (non utilisé ici)."""
        pass

    def chatHandler(self):
        """Point d’extension pour gérer un chat (non utilisé ici)."""
        pass

    def interact(self):
        """
        Récupère une touche depuis la queue `inputs` et déclenche
        un évènement clavier côté page (le JS peut l’écouter).
        """
        try:
            inKey = self.userInputs.get(True, 0.02)
        except Exception:
            return
        self.driver.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown',{'key': arguments[0]}));", inKey
        )

    def unset(self):
        """À surcharger par la sous-classe (ex. Teams.unset()) si besoin."""
        pass

    def run(self):
        """
        Pipeline principal :
        - loadPage() (spécifique à Teams),
        - (optionnel) ensurePrejoinContext(),
        - join() : injection + pré-join,
        - (optionnel) monitorSingleParticipant,
        - boucle d’interaction (interact + chatHandler).
        """
        try:
            self.loadPage()

            # Si la sous-classe propose un contexte pré-join, on l’utilise
            if hasattr(self, "ensurePrejoinContext"):
                try:
                    self.ensurePrejoinContext()
                except Exception:
                    pass

            self.join()

            if os.getenv("ENDING_TIMEOUT"):
                self.monitorSingleParticipant(int(os.getenv("ENDING_TIMEOUT")), checkInterval=60)

            # (Optionnel) vos assets IVR :
            # self.loadImages(os.path.join(os.path.dirname(os.path.normpath(__file__)),'../browsing/assets/'),
            #                 self.config['lang'])
            # self.loadJS(os.path.join(os.path.dirname(os.path.normpath(__file__)),'../browsing/assets/IVR/menu.js'))
            # self.driver.execute_script("menu=new Menu(); menu.img['icon'] = arguments[0]; menu.img['dtmf'] = arguments[1]; menu.show();",
            #                            self.iconB64, self.dtmfB64)

            while self.room:
                self.interact()
                self.chatHandler()
        except Exception as e:
            print("Error while browsing: {}".format(e), flush=True)

    def stop(self):
        """
        Arrêt propre :
        - tente un meeting.leave() si possible,
        - ferme/quit le driver Selenium.
        """
        try:
            self.room={}
            self.name=''
            if self.driver:
                try:
                    if self.driver.execute_script("return !!window.meeting"):
                        self.unset()
                except Exception:
                    pass
                self.driver.close()
                self.driver.quit()
                self.driver = []
                print("Browsing stopped", flush=True)
        except Exception:
            traceback.print_exc(file=sys.stdout)
