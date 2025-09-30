#!/usr/bin/env python
"""
-------------------------------------------------------------
FONCTIONNEMENT GÉNÉRAL (teams.py)
-------------------------------------------------------------
Sous-classe de Browsing spécifique à Microsoft Teams.
- Navigue vers l’URL de réunion (loadPage).
- (Si possible) se place dans l’iframe du pré-join pour que
  le JS injecté voie bien le champ du nom.
- L’injection et l’appel à join() sont faits par Browsing.join().

Ce fichier ajoute surtout le switch d’iframe avant l’injection.
-------------------------------------------------------------
"""
import sys
import time
import traceback

from browsing import Browsing
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

def switch_to_frame_with_selector(driver, css, timeout=30):
    """
    Bascule Selenium dans l'iframe qui contient l’élément `css`.
    - Parcourt les iframes au 1er niveau (suffisant pour Teams dans la majorité des cas).
    - Lève TimeoutException si aucune iframe ne contient le sélecteur avant expiration.
    """
    end = time.time() + timeout
    while time.time() < end:
        driver.switch_to.default_content()
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        for frame in frames:
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(frame)
                if driver.find_elements(By.CSS_SELECTOR, css):
                    return True
            except Exception:
                pass
        time.sleep(0.5)
    raise TimeoutException(f"Iframe avec '{css}' introuvable")

class Teams(Browsing):
    def loadPage(self):
        """
        Ouvre l’URL de la réunion Teams (hébergée derrière webrtc_domain),
        attend que le shell SPA soit prêt, puis tente de se placer dans
        l’iframe qui contient le champ de nom du pré-join.
        """
        self.driver.get("https://{}/{}".format(
            self.room['config']['webrtc_domain'],
            self.room['roomName'].replace('-', '?p=')
        ))

        # Attendre que la page de base soit chargée
        WebDriverWait(self.driver, 30).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        # Essayer de se positionner dans la frame du pré-join
        try:
            switch_to_frame_with_selector(self.driver, "[data-tid='prejoin-display-name-input']", timeout=20)
        except TimeoutException:
            # Pas grave : le JS injecté sait aussi chercher en profondeur
            pass

    def chatHandler(self):
        """(Placeholder) Gestion de chat si besoin côté Python."""
        pass

    def unset(self):
        """
        Lors de l’arrêt, tente un `meeting.leave()` côté JS pour raccrocher proprement.
        """
        try:
            self.driver.execute_script("if (window.meeting) { window.meeting.leave(); }")
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            print("Erreur meeting.leave(): {}".format(e), flush=True)
