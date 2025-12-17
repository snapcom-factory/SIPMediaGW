class Menu {
  constructor() {
    this.meeting = window.meeting;
    this.overlayTimeouts = {};
    this.img = { icon: null, dtmf: null };
    const blocker = document.createElement("div");
    blocker.id = "blocker";
    blocker.tabIndex = 0;
    blocker.style.position = "fixed";
    blocker.style.top = "0";
    blocker.style.left = "0";
    blocker.style.width = "100vw";
    blocker.style.height = "100vh";
    blocker.style.zIndex = "9999";
    document.body.appendChild(blocker);
    this.blocker = blocker;
    this.config = window.menuConfig || {};
  }

  fetchWithTimeout(resource, options = {}, timeout = 5000, onError = null) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => {
      controller.abort();
      if (onError) onError("timeout");
    }, timeout);

    options = { ...options, signal: controller.signal };

    return fetch(resource, options)
      .then((response) => {
        clearTimeout(timeoutId);
        return response;
      })
      .catch((err) => {
        if (onError) onError(err);
        throw err;
      });
  }

  createOverlayImage(imgKey, bt, left, timeout = 0) {
    var imId = "menu_" + imgKey;
    let existing = document.getElementById(imId);
    if (!existing) {
      const img = document.createElement("img");
      img.id = imId;
      img.src = this.img[imgKey];
      img.style.position = "fixed";
      img.style.bottom = bt;
      img.style.left = left;
      img.style.zIndex = "9999";
      document.body.appendChild(img);
    } else {
      existing.style.display = "block";
    }
    if (timeout > 0) {
      setTimeout(() => this.hideOverlayImage(imId), timeout);
    }
  }

  async createOverlayMenu(imgKey, bt, left, timeout = 0) {
    var menuId = "menu_" + imgKey;
    let existing = document.getElementById(menuId);
    if (!existing) {
      const menu = document.createElement("div");
      menu.id = menuId;
      menu.style.position = "fixed";
      menu.style.backgroundColor = "white";
      menu.style.borderRadius = "10px";
      menu.style.bottom = bt;
      menu.style.left = left;
      menu.style.zIndex = "9999";
      menu.style.padding = "10px";
      document.body.appendChild(menu);
      menu.appendChild(this.createMenuContainer());
    } else {
      existing.style.display = "block";
    }
    if (timeout > 0) {
      setTimeout(() => this.hideOverlayMenu(menuId), timeout);
    }
  }

  createMenuContainer() {
    const container = document.createElement("div");
    Object.assign(container.style, {
      backgroundColor: "white",
      borderRadius: "10px",
      color: "black",
      padding: "15px",
    });

    const column = document.createElement("div");
    Object.assign(column.style, {
      display: "flex",
      flexDirection: "column",
      gap: "10px",
    });

    const dtmfOptions = this.getDtmfOptions();
    dtmfOptions?.forEach((option) => {
      column.appendChild(this.createFlexLine(option));
    });

    container.appendChild(column);
    return container;
  }

  createFlexLine(option) {
    const lang = this.config.lang || "fr";
    const line = document.createElement("div");
    Object.assign(line.style, {
      display: "flex",
      justifyContent: "space-between",
      alignItems: "center",
      gap: "20px",
    });

    const img = document.createElement("img");
    img.src = `images/icons/${option.icon}_icon.png`;
    img.style.width = "25px";

    const label = document.createElement("span");
    label.textContent = option[lang];
    label.style.flexGrow = "1";

    const badge = document.createElement("span");
    badge.textContent = option.dtmf;
    Object.assign(badge.style, {
      backgroundColor: "#000091",
      color: "white",
      width: "20px",
      height: "20px",
      borderRadius: "50%",
      display: "flex",
      justifyContent: "center",
      alignItems: "center",
      fontSize: "12px",
    });

    line.appendChild(img);
    line.appendChild(label);
    line.appendChild(badge);

    return line;
  }

  getDtmfOptions() {
    const domains = this.config.webrtc_domains;
    const webrtcDomainsArray = Object.entries(domains).map(([key, value]) => ({
      id: key,
      ...value,
    }));
    const service = webrtcDomainsArray.find(
      (s) => s.domain === this.meeting.domain
    );
    return service ? service.options : null;
  }

  showOverlayMenu(id) {
    const menu = document.getElementById(id);
    if (menu) {
      menu.style.display = "block";
    }
  }

  hideOverlayMenu(id) {
    const menu = document.getElementById(id);
    if (menu) {
      menu.style.display = "none";
    }
  }

  toggleOverlayMenu(id, timeout = 5000) {
    const menu = document.getElementById(id);
    if (menu && menu.style.display == "block") {
      this.hideOverlayMenu(id);
      clearTimeout(this.overlayTimeouts[id]);
    } else {
      this.showOverlayMenu(id, 0);
      if (this.overlayTimeouts[id]) {
        clearTimeout(this.overlayTimeouts[id]);
      }
      this.overlayTimeouts[id] = setTimeout(() => {
        this.hideOverlayMenu(id);
        delete this.overlayTimeouts[id];
      }, timeout);
    }
  }

  interact(key) {
    if (key == "#") {
      this.toggleOverlayMenu("menu_dtmf", 5000);
    } else {
      this.meeting.interact(key);
    }
  }

  showOverlayImage(id) {
    const img = document.getElementById(id);
    if (img) {
      img.style.display = "block";
    }
  }

  hideOverlayImage(id) {
    const img = document.getElementById(id);
    if (img) {
      img.style.display = "none";
    }
  }

  toggleOverlayImage(id, timeout = 5000) {
    const img = document.getElementById(id);
    if (img && img.style.display == "block") {
      this.hideOverlayImage(id);
      clearTimeout(this.overlayTimeouts[id]);
    } else {
      this.showOverlayImage(id, 0);
      if (this.overlayTimeouts[id]) {
        clearTimeout(this.overlayTimeouts[id]);
      }
      this.overlayTimeouts[id] = setTimeout(() => {
        this.hideOverlayImage(id);
        delete this.overlayTimeouts[id];
      }, timeout);
    }
  }

  async show() {
    this.createOverlayImage("icon", "20px", "20px");
    console.log("Menu config loaded:", this.config);
    this.createOverlayMenu("dtmf", "10px", "10px", 5000);
    this.blocker.focus();
    this.blocker.addEventListener("blur", () => {
      setTimeout(() => this.blocker.focus(), 0);
    });

    try {
      document.addEventListener(
        "keydown",
        (e) => {
          const key = e.key;
          if ((key >= "0" && key <= "9") || key === "#" || key === "s") {
            e.preventDefault();
            this.interact(key);
          }
        },
        { capture: true }
      );
    } catch (err) {
      console.error("Error during Menu setup:", err);
      onError(err);
    }
  }
}

window.Menu = Menu;
