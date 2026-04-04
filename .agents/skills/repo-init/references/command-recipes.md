# Repo-init command recipes

These are command patterns for the skill. Prefer the probe script first.

## Probe

POSIX:

```bash
python3 .agents/skills/repo-init/scripts/repo_init_probe.py
```

Windows:

```powershell
py -3 .agents/skills/repo-init/scripts/repo_init_probe.py
```

## Official `gh` installs

### macOS with Homebrew

```bash
brew install gh
```

If Homebrew is missing:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install gh
```

### Ubuntu / Debian / WSL

```bash
(type -p wget >/dev/null || (sudo apt update && sudo apt install wget -y))         && sudo mkdir -p -m 755 /etc/apt/keyrings         && out=$(mktemp) && wget -nv -O$out https://cli.github.com/packages/githubcli-archive-keyring.gpg         && cat $out | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null         && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg         && sudo mkdir -p -m 755 /etc/apt/sources.list.d         && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main"            | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null         && sudo apt update         && sudo apt install gh -y
```

### Windows with `winget`

```powershell
winget install --id GitHub.cli
```

## No-admin fallback installs

### POSIX

```bash
python3 .agents/skills/repo-init/scripts/install_gh_user.py
```

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File .agents/skills/repo-init/scripts/install-gh-user.ps1
```

## Auth

Preferred interactive login:

```bash
gh auth login --hostname github.com --git-protocol ssh --web
```

Headless token login:

```bash
gh auth login --hostname github.com --git-protocol ssh --with-token < mytoken.txt
```

Or use an environment token and then verify:

```bash
gh auth status --hostname github.com
gh api user --jq .login
```

If HTTPS Git is chosen instead of SSH, configure Git credential integration:

```bash
gh auth setup-git
```

To force Git protocol preference per host:

```bash
gh config set git_protocol ssh --host github.com
```

## SSH key path

Let `gh auth login --git-protocol ssh` detect, create, and upload a key when possible.

If a manual path is needed:

```bash
ssh-keygen -t ed25519 -C "your-email@example.com" -f ~/.ssh/id_ed25519 -N ""
gh ssh-key add ~/.ssh/id_ed25519.pub -t "$(hostname)-repo-init"
```

## Recursive submodules

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

## Fork creation and sync

Create a fork without letting `gh` rewrite local remotes automatically:

```bash
gh repo fork OWNER/REPO --remote=false --default-branch-only
```

Sync a fork to the community default branch:

```bash
gh repo sync USER/REPO --source OWNER/REPO
```

## Remote wiring

Add or update remotes conservatively:

```bash
git remote add upstream https://github.com/OWNER/REPO.git
git remote set-url origin git@github.com:USER/REPO.git
git remote set-url upstream https://github.com/OWNER/REPO.git
```

If a remote name is occupied and the user wants to preserve it, rename instead of deleting:

```bash
git remote rename upstream upstream-old
```

## Branch placement

Fast-forward, track a remote branch, and avoid destructive resets by default:

```bash
git fetch origin --prune
git switch main || git switch -c main --track origin/main
git branch --set-upstream-to=origin/main main
git pull --ff-only
```

Community-only mode may use a community remote in place of `origin`.

Never use the following without explicit approval:

```bash
git reset --hard origin/main
```

## `gh` default repo

```bash
gh repo set-default upstream
```
