class Visio {
    constructor(domain, roomName, displayName, lang, token, audioOnly) {
        this.domain = domain;
        this.roomName = roomName;
        this.displayName = displayName;
        this.lang = lang;
        this.token = token;
        this.isZeroPrefix = false;
    }

    // Utility: Wait for an element to be clickable
    async waitForElement(selector, { visible = false, clickable = false } = {}, timeout = 20000) {
        const start = Date.now();

        return new Promise((resolve, reject) => {
            const interval = setInterval(() => {
                const el = document.querySelector(selector);
                if (!el) return;
                const style = window.getComputedStyle(el);
                const isVisible = style.display !== 'none' && style.visibility !== 'hidden' && el.offsetHeight > 0 && el.offsetWidth > 0;
                const isEnabled = !el.disabled;
                if (
                    (!visible || isVisible) &&
                    (!clickable || (isVisible && isEnabled))
                ) {
                    clearInterval(interval);
                    resolve(el);
                }
                if (Date.now() - start > timeout) {
                    clearInterval(interval);
                    reject(new Error(`Timeout: ${selector} not matching criteria`));
                }
            }, 100);
        });
    }

    async join() {
        try {
            debugger;
            console.log('[INFO] Waiting for display name input...');
            const nameInput = await this.waitForElement("input[type='text']", { visible: true });
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeInputValueSetter.call(nameInput, this.displayName);
            nameInput.dispatchEvent(new Event('input', { bubbles: true }));
            console.log('[✓] Name field detected and filled');
            console.log('[INFO] Submitting join form...');
            const joinButton = await this.waitForElement("button[type='submit']", { clickable: true });
            joinButton.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            console.log('[✓] Join form submitted');
        } catch (error) {
            console.error('[✗] Prejoin process failed:', error);
        }
    }

    interact(key) {
        // Fonction utilitaire pour simuler l'appui sur Ctrl + Touche
        const k = String(key);
        if (k === "0") {
            this.isZeroPrefix = true;
            // On annule automatiquement après 2 secondes si rien n'est pressé
            setTimeout(() => { this.isZeroPrefix = false; }, 2000);
            return;
        }

        // Fonction utilitaire pour envoyer les combinaisons de touches
        const sendKey = (letter, useShift = false) => {
            const lowerLetter = letter.toLowerCase();
            const event = new KeyboardEvent('keydown', {
                key: useShift ? lowerLetter.toUpperCase() : lowerLetter,
                code: 'Key' + lowerLetter.toUpperCase(),
                ctrlKey: true,    // Simule Ctrl
                shiftKey: useShift, // Simule Maj (Shift)
                bubbles: true,
                cancelable: true,
                view: window
            });
            document.dispatchEvent(event);
        };

    
        if (this.isZeroPrefix) {
            this.isZeroPrefix = false; // On consomme l'étoile immédiatement
            
            switch (k) {
                case "1": // Reaction : Thumbs Up (Ctrl+Shift+E)
                    sendKey('E', true);
                    this.waitForElement('button[data-attr="send-reaction-thumbs-up"]', { visible: true, clickable: true }, 2000)
                    .then(btn => {
                        // Click on emoji
                        btn.click();

                        // wait 100 ms before closing reaction panel
                        setTimeout(() => {
                            sendKey('E', true);
                        }, 100);
                    })
                    .catch(() => {
                        
                        sendKey('E', true);
                    });
                    break;
                case "2": // Reaction : Heart (Ctrl+Shift+E)
                    sendKey('E', true);
                    this.waitForElement('button[data-attr="send-reaction-red-heart"]', { visible: true, clickable: true }, 2000)
                    .then(btn => {
                        // Click on emoji
                        btn.click();

                        // wait 100 ms before closing reaction panel
                        setTimeout(() => {
                            sendKey('E', true);
                        }, 100);
                    })
                    .catch(() => {
                        sendKey('E', true);
                    });
                    break;
                case "3": // Reaction : Applause (Ctrl+Shift+E)
                    sendKey('E', true);
                    this.waitForElement('button[data-attr="send-reaction-clapping-hands"]', { visible: true, clickable: true }, 2000)
                    .then(btn => {
                        // Click on emoji
                        btn.click();

                        // wait 100 ms before closing reaction panel
                        setTimeout(() => {
                            sendKey('E', true);
                        }, 100);
                    })
                    .catch(() => {
                        sendKey('E', true);
                    });
                    break;
                case "4": // Reaction : Laugh (Ctrl+Shift+E)
                    sendKey('E', true);
                    this.waitForElement('button[data-attr="send-reaction-face-with-tears-of-joy"]', { visible: true, clickable: true }, 2000)
                    .then(btn => {
                        // Click on emoji
                        btn.click();

                        // wait 100 ms before closing reaction panel
                        setTimeout(() => {
                            sendKey('E', true);
                        }, 100);
                    })
                    .catch(() => {
                        sendKey('E', true);
                    });
                    break;
            }
        } else {
            switch (key) {
            case "1": // Micro (Ctrl+d)
            sendKey('d', false);
                break;
            case "2": // Camera (Ctrl+e)
                sendKey('e', false);
                break;
            case "3": // Chat (Ctrl+Shift+M)
                sendKey('M', true);
                break; 
            case "4": // Hand (Ctrl+Shift+H)
                sendKey('H', true);
                break; 
            case "5": // Participants (Ctrl+Shift+P)
                sendKey('P', true);
                break; 
            case "6": // Full Screen (Ctrl+Shift+F)
                sendKey('F', true);
                break; 
            case "7": // Recording (Ctrl+Shift+L)
                sendKey('L', true);
                break; 
            case "s":
                dispatchShortcut('s');
                break;
        }
        }
    }

    async leave() {
        console.log('[INFO] Leave the meeting room');
        try {
            document.querySelector('[data-attr*="controls-leave"]').click();
        } catch (e) {
            console.error('[✗] Logout failed:', e);
        }
    }






}

window.Visio = Visio;
window.Browsing = Visio;