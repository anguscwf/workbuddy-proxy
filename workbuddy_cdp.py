"""Shared, side-effect-free WorkBuddy CDP target and auth compatibility."""

from urllib.parse import unquote, urlparse


def is_workbuddy_target(target: object) -> bool:
    """Recognize legacy workbench pages and the installed desktop renderer."""
    if not isinstance(target, dict) or target.get("type") != "page":
        return False
    websocket_url = target.get("webSocketDebuggerUrl")
    url = target.get("url")
    if not isinstance(websocket_url, str) or not websocket_url.strip():
        return False
    if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    path = unquote(parsed.path).replace("\\", "/").lower()
    if not path.startswith("/") or path.startswith("//") or any(part in (".", "..") for part in path.split("/")) or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return False
    if parsed.scheme.lower() == "file" and not parsed.netloc:
        return path.endswith("/workbuddy/resources/app.asar/renderer/index.html")
    if parsed.scheme.lower() == "vscode-file" and parsed.netloc.lower() == "vscode-app":
        return any(path.endswith("/workbuddy/resources/app/out/vs/code/" + variant + "/workbench/workbench.html")
                   for variant in ("electron-browser", "electron-sandbox"))
    return False


def select_workbuddy_target(targets: object) -> dict | None:
    if not isinstance(targets, list):
        return None
    return next((target for target in targets if is_workbuddy_target(target)), None)


AUTH_EXPRESSION = """
    (async () => {
        const normalizeToken = (token) => {
            const valid = value => typeof value === 'string' && value.trim().length > 0;
            const accessToken = valid(token) ? token
                : valid(token?.accessToken) ? token.accessToken
                : valid(token?.token) ? token.token : '';
            return {
                accessToken: typeof accessToken === 'string' ? accessToken : '',
                refreshToken: typeof token?.refreshToken === 'string'
                    ? token.refreshToken : ''
            };
        };
        const normalizeSession = session => {
            if (session?.error) return null;
            const auth = normalizeToken(session?.auth || session);
            if (!auth.accessToken) return null;
            const result = {...auth};
            if (session?.account && typeof session.account === 'object' && !Array.isArray(session.account)) {
                result.account = session.account;
            }
            return result;
        };
        // WorkBuddy Desktop 5.x exposes registered daemon APIs through this
        // bridge. auth:getToken returns an access-token string or null;
        // it preserves the host's own authentication/storage checks.
        const readers = [
            () => {
                const providers = window.__GENIE_DEFAULT_APP_PROVIDERS__;
                return typeof providers?.auth?.getToken === 'function'
                    ? providers.auth.getToken() : null;
            },
            () => typeof window.__wbInvoke === 'function' ? window.__wbInvoke('auth:getToken') : null,
            () => typeof window.vscode?.ipcRenderer?.invoke === 'function'
                ? window.vscode.ipcRenderer.invoke('vscode:genie:auth:getSession') : null,
        ];
        for (const read of readers) {
            try {
                const session = normalizeSession(await read());
                if (session) return JSON.stringify(session);
            } catch (_) {
                // A missing legacy provider must not hide the current bridge.
            }
        }
        return JSON.stringify({error: 'WorkBuddy auth session unavailable'});
    })()
"""
