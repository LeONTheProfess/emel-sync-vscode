import * as vscode from "vscode";
import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

let output: vscode.OutputChannel;
let running: ChildProcessWithoutNullStreams | undefined;

function workspaceRoot(): string | undefined {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function setting<T>(key: string): T | undefined {
    return vscode.workspace.getConfiguration("emel").get<T>(key);
}

function pythonExecutable(): string {
    const configured = setting<string>("pythonPath");
    if (configured?.trim()) return configured.trim();
    return process.platform === "win32" ? "python" : "python3";
}

function scriptPath(context: vscode.ExtensionContext): string {
    const configured = setting<string>("scriptPath");
    if (configured?.trim()) {
        return path.resolve(
            workspaceRoot() ?? process.cwd(),
            configured
        );
    }
    return path.join(context.extensionPath, "python", "emel_sync.py");
}

function append(data: Buffer | string): void {
    output.append(data.toString());
}

function configPath(root: string): string {
    return path.join(root, ".emel", "config.json");
}

function repositoryInitialized(root: string): boolean {
    return fs.existsSync(configPath(root));
}

function quoteArg(value: string): string {
    if (/^[a-zA-Z0-9_./:*\\-]+$/.test(value)) return value;
    return JSON.stringify(value);
}

async function runPython(
    context: vscode.ExtensionContext,
    args: string[],
    title: string
): Promise<number> {
    const root = workspaceRoot();

    if (!root) {
        vscode.window.showErrorMessage("EMEL: откройте папку проекта в VS Code.");
        return 2;
    }

    if (running) {
        vscode.window.showWarningMessage("EMEL: другая операция уже выполняется.");
        return 3;
    }

    const python = pythonExecutable();
    const script = scriptPath(context);
    const fullArgs = [script, ...args];

    output.clear();
    output.show(true);
    output.appendLine(`$ ${python} ${fullArgs.map(quoteArg).join(" ")}`);
    output.appendLine("");

    const terminal = setting<boolean>("showTerminal")
        ? vscode.window.createTerminal("EMEL Sync")
        : undefined;

    terminal?.show(true);

    return vscode.window.withProgress(
        {
            location: vscode.ProgressLocation.Notification,
            title,
            cancellable: true
        },
        async (progress, token) => {
            progress.report({ message: "Запуск emel_sync.py..." });

            return new Promise<number>((resolve) => {
                running = spawn(python, fullArgs, {
                    cwd: root,
                    env: { ...process.env },
                    windowsHide: true
                });

                const child = running;

                child.stdout.on("data", append);
                child.stderr.on("data", append);

                const cancel = () => {
                    if (!running) return;

                    output.appendLine("");
                    output.appendLine("Отмена пользователем...");

                    if (process.platform === "win32" && child.pid) {
                        spawn("taskkill", [
                            "/pid", String(child.pid), "/t", "/f"
                        ]);
                    } else {
                        child.kill("SIGTERM");
                    }
                };

                token.onCancellationRequested(cancel);

                child.on("error", (err) => {
                    output.appendLine(`Ошибка запуска Python: ${err.message}`);
                    running = undefined;
                    resolve(2);
                });

                child.on("close", (code) => {
                    running = undefined;
                    resolve(code ?? 2);
                });
            });
        }
    );
}

async function initializeRepository(
    context: vscode.ExtensionContext
): Promise<boolean> {
    const root = workspaceRoot();

    if (!root) {
        vscode.window.showErrorMessage(
            "EMEL: сначала откройте папку проекта в VS Code."
        );
        return false;
    }

    if (repositoryInitialized(root)) {
        const action = await vscode.window.showInformationMessage(
            "EMEL: эта папка уже инициализирована.",
            "Открыть config.json",
            "Отмена"
        );

        if (action === "Открыть config.json") {
            await openConfig();
        }
        return true;
    }

    const url = await vscode.window.showInputBox({
        title: "EMEL — API URL",
        prompt: "Введите URL API вашего WMS",
        placeHolder: "http://server/api",
        ignoreFocusOut: true,
        validateInput: (value) => {
            if (!value.trim()) return "URL не может быть пустым.";
            try {
                new URL(value);
                return undefined;
            } catch {
                return "Введите корректный URL.";
            }
        }
    });

    if (!url) return false;

    const token = await vscode.window.showInputBox({
        title: "EMEL — Token",
        prompt: "Введите токен WMS",
        password: true,
        ignoreFocusOut: true,
        validateInput: (value) =>
            value.trim() ? undefined : "Токен не может быть пустым."
    });

    if (!token) return false;

    const code = await runPython(
        context,
        ["init", root, "--url", url.trim(), "--token", token],
        "EMEL: инициализация"
    );

    if (code === 0) {
        vscode.window.showInformationMessage(
            "EMEL: репозиторий успешно инициализирован."
        );
        return true;
    }

    vscode.window.showErrorMessage(
        `EMEL: инициализация завершилась с кодом ${code}.`
    );
    return false;
}

async function ensureRepository(
    context: vscode.ExtensionContext,
    root: string
): Promise<boolean> {
    if (repositoryInitialized(root)) return true;

    const action = await vscode.window.showWarningMessage(
        "В текущей папке ещё не инициализирован EMEL-репозиторий.",
        "Initialize",
        "Отмена"
    );

    if (action === "Initialize") {
        return initializeRepository(context);
    }

    return false;
}

async function sync(
    context: vscode.ExtensionContext,
    args: string[] = []
): Promise<void> {
    const root = workspaceRoot();

    if (!root) {
        vscode.window.showErrorMessage("EMEL: откройте папку проекта в VS Code.");
        return;
    }

    if (!(await ensureRepository(context, root))) return;

    const workers = setting<number>("defaultWorkers") ?? 2;

    const code = await runPython(
        context,
        [
            "sync",
            root,
            ...args,
            "--workers",
            String(Math.max(1, Math.floor(workers)))
        ],
        "EMEL: синхронизация"
    );

    if (code === 0) {
        vscode.window.showInformationMessage(
            "EMEL: синхронизация завершена."
        );
    } else if (code === 1) {
        vscode.window.showWarningMessage(
            "EMEL: синхронизация завершена, но часть классов не скачалась. Подробности — в EMEL Sync."
        );
    } else if (code !== 3) {
        vscode.window.showErrorMessage(
            `EMEL: синхронизация завершилась с кодом ${code}. Подробности — в EMEL Sync.`
        );
    }
}

async function initAndSync(
    context: vscode.ExtensionContext
): Promise<void> {
    const root = workspaceRoot();

    if (!root) {
        vscode.window.showErrorMessage("EMEL: откройте папку проекта в VS Code.");
        return;
    }

    if (!repositoryInitialized(root)) {
        const initialized = await initializeRepository(context);
        if (!initialized) return;
    }

    await sync(context);
}

async function openConfig(): Promise<void> {
    const root = workspaceRoot();

    if (!root) {
        vscode.window.showErrorMessage("EMEL: откройте папку проекта.");
        return;
    }

    const file = vscode.Uri.file(configPath(root));

    if (!fs.existsSync(file.fsPath)) {
        vscode.window.showErrorMessage(
            "EMEL: .emel/config.json не найден."
        );
        return;
    }

    await vscode.window.showTextDocument(
        await vscode.workspace.openTextDocument(file)
    );
}

export function activate(context: vscode.ExtensionContext): void {
    output = vscode.window.createOutputChannel("EMEL Sync");

    context.subscriptions.push(
        output,
        vscode.commands.registerCommand(
            "emel.init",
            () => initializeRepository(context)
        ),
        vscode.commands.registerCommand(
            "emel.initAndSync",
            () => initAndSync(context)
        ),
        vscode.commands.registerCommand(
            "emel.sync",
            () => sync(context)
        ),
        vscode.commands.registerCommand(
            "emel.fullSync",
            () => sync(context, ["--full"])
        ),
        vscode.commands.registerCommand(
            "emel.dryRun",
            () => sync(context, ["--dry-run"])
        ),
        vscode.commands.registerCommand(
            "emel.openConfig",
            () => openConfig()
        )
    );

    output.appendLine(
        `EMEL Sync активирован (${os.platform()} ${os.arch()}).`
    );
}

export function deactivate(): void {
    if (!running) return;

    if (process.platform === "win32" && running.pid) {
        spawn("taskkill", [
            "/pid", String(running.pid), "/t", "/f"
        ]);
    } else {
        running.kill("SIGTERM");
    }

    running = undefined;
}
