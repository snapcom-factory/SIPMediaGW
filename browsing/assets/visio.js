class Visio extends UIHelper{
    constructor(domain, roomName, displayName, lang, prompts, token, audioOnly) {
        super();
        this.domain = domain;
        this.roomName = roomName;
        this.displayName = displayName;
        this.lang = lang;
        this.token = token;
        this.joined = false;
        this.passwordPrompt = JSON.parse(prompts)[lang]['password'];
        this.isZeroPrefix = false;
        this.zeroPrefixTimer = null;
    }

    async join() {
        try {
            console.log('[INFO] Waiting for display name input...');
            let nameInput;
            try {
                nameInput = await this.waitForElement("input[type='text']", { visible: true });
            } catch (e) {
                console.error('[✗] Name field not found:', e);
                return;
            }
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            nativeInputValueSetter.call(nameInput, this.displayName);
            nameInput.dispatchEvent(new Event('input', { bubbles: true }));
            console.log('[✓] Name field detected and filled');

            console.log('[INFO] Submitting join form...');
            let joinButton;
            try {
                joinButton = await this.waitForElement("button[type='submit']", { clickable: true });
            } catch (e) {
                console.error('[✗] Join button not found:', e);
                return;
            }
            joinButton.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
            console.log('[✓] Join form submitted');
            this.blockerFocus();
            this.joined = true;
        } catch (error) {
            console.error('[✗] Prejoin process failed:', error);
        }
    }

    sendCtrlShortcut(letter, useShift = false) {
        const k = String(letter).toLowerCase();
        const event = new KeyboardEvent('keydown', {
            key: useShift ? k.toUpperCase() : k,
            code: 'Key' + k.toUpperCase(),
            ctrlKey: true,
            shiftKey: useShift,
            bubbles: true,
            cancelable: true,
            view: window
        });
        document.dispatchEvent(event);
    }

    clickIfExists(selector) {
        const element = document.querySelector(selector);
        if (element)
            element.click();
    }

    sendReaction(selector) {
        this.sendCtrlShortcut('e', true);
        this.waitForElement(selector, { visible: true, clickable: true }, 2000)
            .then((button) => {
                button.click();
                // Close reaction panel shortly after sending the reaction.
                setTimeout(() => this.sendCtrlShortcut('e', true), 100);
            })
            .catch(() => {
                this.sendCtrlShortcut('e', true);
            });
    }

    interact(key) {
        const k = String(key).toLowerCase();
        if (k === "0") {
            this.isZeroPrefix = true;
            clearTimeout(this.zeroPrefixTimer);
            this.zeroPrefixTimer = setTimeout(() => {
                this.isZeroPrefix = false;
            }, 2000);
            return;
        }

        if (this.isZeroPrefix) {
            this.isZeroPrefix = false;
            clearTimeout(this.zeroPrefixTimer);
            switch (k) {
                case "1":
                    this.sendReaction('button[data-attr="send-reaction-thumbs-up"]');
                    return;
                case "2":
                    this.sendReaction('button[data-attr="send-reaction-red-heart"]');
                    return;
                case "3":
                    this.sendReaction('button[data-attr="send-reaction-clapping-hands"]');
                    return;
                case "4":
                    this.sendReaction('button[data-attr="send-reaction-face-with-tears-of-joy"]');
                    return;
                default:
                    // Unknown prefixed command: continue with normal handling.
                    break;
            }
        }

        switch (k) {
            case "1":
                this.sendCtrlShortcut('d');
                this.clickIfExists('[aria-label*="Ctrl+d"]');
                break;
            case "2":
                this.sendCtrlShortcut('e');
                this.clickIfExists('[aria-label*="Ctrl+e"]');
                break;
            case "3":
            case "c":
                this.sendCtrlShortcut('m', true);
                this.clickIfExists('button[data-attr*="controls-chat-closed"], button[data-attr*="controls-chat-open"]');
                break;
            case "4":
                this.sendCtrlShortcut('h', true);
                this.clickIfExists('button[data-attr*="controls-hand-raise"], button[data-attr*="controls-hand-lower"]');
                break;
            case "5":
                this.sendCtrlShortcut('p', true);
                this.clickIfExists('button[data-attr*="controls-participants-closed"], button[data-attr*="controls-participants-open"]');
                break;
            case "6":
                this.sendCtrlShortcut('f', true);
                break;
            case "7":
                this.sendCtrlShortcut('l', true);
                break;
            case "s":
                this.clickIfExists('[data-attr*="controls-screenshare"]');
                break;
            default:
                break;
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
