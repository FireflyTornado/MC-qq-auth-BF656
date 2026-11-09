#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export LC_ALL=C GIT_TERMINAL_PROMPT=0 GIT_OPTIONAL_LOCKS=0

readonly PROJECT=/opt/mc-qq-auth BACKUPS=/opt/mc-qq-auth-backups
readonly REPO=FireflyTornado/MC-qq-auth-BF656
readonly API=mc-qq-api.service BOT=mc-qq-bot.service
readonly DB="$PROJECT/bot/data/auth.db" PYTHON="$PROJECT/bot/.venv/bin/python"
readonly -a PROTECTED=(bot/.env napcat/.env bot/data napcat/data bot/.venv)
mode=check
local_only=0
phase=preflight
head= target= scratch= incomplete_backup= verified_backup=
declare -a attempted_starts=()

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
repo_git() { git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }

finish() {
    local status=$1 i
    trap - EXIT
    set +e
    if (( status != 0 )) && [[ $mode == apply ]]; then
        printf 'Upgrade stopped in phase %s. Previous=%s target=%s.\n' \
            "$phase" "$head" "$target" >&2
        if [[ -n $verified_backup ]]; then
            printf 'Latest verified backup: %s\n' "$verified_backup" >&2
        fi
        for ((i=${#attempted_starts[@]}-1; i>=0; i--)); do
            if ! systemctl stop "${attempted_starts[i]}" >/dev/null 2>&1; then
                printf 'Confirm %s is stopped before recovery.\n' "${attempted_starts[i]}" >&2
            fi
        done
        case $phase in
            preflight|wheel-download|backup) log 'Source and services have not been changed.' ;;
            stop|offline-backup) log 'Source is unchanged. Check stopped services before restarting.' ;;
            git-update) log 'Inspect HEAD and worktree before restarting. Services stopped by this run stay stopped.' ;;
            dependencies) log 'Source was updated. Verify or rebuild the virtual environment before restarting.' ;;
            migration) log 'Source was updated. Check the database version and verified backup before restarting.' ;;
            restart|health) log 'Review service status, effective units and logs locally before restarting.' ;;
        esac
        log 'See docs/deployment.md for manual recovery. No source or database rollback was attempted.'
    fi
    if [[ -n $incomplete_backup && $incomplete_backup == "$BACKUPS"/auth-*.db ]]; then
        rm -f -- "$incomplete_backup"
    fi
    if [[ -n $scratch && $scratch == /tmp/mc-qq-auth-update.* && -d $scratch && ! -L $scratch ]]; then
        rm -rf -- "$scratch"
    fi
    exit "$status"
}

usage() {
    log 'Usage: bash deploy/update.sh [--check [--local] | --apply <commit-sha>]'
}

case ${1-} in
    '') (( $# == 0 )) || die 'Invalid arguments.' ;;
    --check)
        if [[ ${2-} == --local && $# == 2 ]]; then local_only=1
        elif (( $# != 1 )); then die 'Invalid check arguments.'
        fi
        ;;
    --apply)
        [[ $# == 2 && $2 =~ ^[0-9a-fA-F]{7,40}$ ]] || die 'Specify a 7 to 40 digit hexadecimal commit SHA.'
        mode=apply
        requested=${2,,}
        ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
esac
trap 'finish "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

for command in git id flock systemctl docker sqlite3 timeout; do
    command -v "$command" >/dev/null || die "Missing command: $command"
done
if [[ $mode == apply ]]; then
    [[ $(id -u) == 0 ]] || die 'Apply requires root privileges.'
    for command in curl ss awk journalctl stat install mktemp date mkdir rm sleep; do
        command -v "$command" >/dev/null || die "Missing command: $command"
    done
fi
# Keep Git bound to this physical checkout and its ordinary index.
for variable in GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_NAMESPACE \
    GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT GIT_CONFIG; do
    [[ ! -v $variable ]] || die "Unset Git override: $variable"
done
[[ $(pwd -P) == "$PROJECT" && -d .git && ! -L .git ]] || die "Run from $PROJECT."
worktree=$(repo_git rev-parse --show-toplevel) || die 'Unable to identify Git worktree.'
[[ $(cd -- "$worktree" && pwd -P) == "$PROJECT" ]] || die 'Unexpected Git worktree.'
exec {lock_fd}<.git
flock -n "$lock_fd" || die 'Another upgrade or check holds the repository lock.'
[[ $(repo_git symbolic-ref --quiet --short HEAD) == main ]] || die 'Expected branch main.'
[[ $(repo_git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}') == origin/main ]] ||
    die 'Expected main to track origin/main.'
head=$(repo_git rev-parse --verify HEAD)
origin=$(repo_git remote get-url --all origin)
case "$origin" in
    "https://github.com/$REPO"|"https://github.com/$REPO.git"|"git@github.com:$REPO"|"git@github.com:$REPO.git"| \
    "ssh://git@github.com/$REPO"|"ssh://git@github.com/$REPO.git") ;;
    *) die 'Origin is not the expected GitHub repository.' ;;
esac
log "Worktree $PROJECT, main $head, origin $REPO."
clean_worktree() {
    local marker changes flags
    for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply sequencer; do
        [[ ! -e .git/$marker && ! -L .git/$marker ]] || die 'An unfinished Git operation exists.'
    done
    flags=$(repo_git ls-files -v) || die 'Unable to inspect index flags.'
    if [[ $flags =~ (^|$'\n')[a-zS]\  ]]; then
        die 'Index contains assume-unchanged or skip-worktree entries.'
    fi
    changes=$(repo_git status --porcelain=v1 --untracked-files=all) || die 'Unable to inspect worktree.'
    [[ -z $changes ]] || die 'Worktree has local modifications or non-ignored untracked files.'
}
clean_worktree
tracked=$(repo_git ls-files -- "${PROTECTED[@]}")
[[ -z $tracked ]] || die 'A protected runtime path is tracked in Git.'

for path in bot napcat bot/data napcat/data bot/.venv; do
    [[ -d $path && ! -L $path ]] || die "Expected a real directory: $path"
done
for path in bot/.env napcat/.env bot/data/auth.db; do
    [[ -f $path && -s $path && ! -L $path && -r $path ]] || die "Missing, empty or unsafe runtime file: $path"
done
[[ -x $PYTHON ]] || die 'Production virtual environment interpreter is unavailable.'
for path in "$DB-wal" "$DB-shm" "$DB-journal"; do
    [[ ! -L $path && ( ! -e $path || -f $path ) ]] || die 'Unsafe SQLite sidecar file.'
done
log 'Protected configuration and database files are present and nonempty.'

cached=$(repo_git rev-parse --verify 'refs/remotes/origin/main^{commit}') || die 'Missing cached origin/main.'
remote=$cached
if (( local_only )); then
    log "Local-only check. Cached origin/main $cached. GitHub freshness is unknown."
else
    log 'Fetching main updates Git objects and FETCH_HEAD. origin/main advances only after ancestry validation.'
    repo_git fetch --quiet --no-tags --no-prune --no-prune-tags --no-auto-maintenance \
        --no-write-commit-graph --no-recurse-submodules --refmap= origin refs/heads/main \
        >/dev/null 2>&1 || die 'Git fetch failed. Check GitHub authentication and connectivity.'
    remote=$(repo_git rev-parse --verify 'FETCH_HEAD^{commit}') || die 'Fetched main is not a commit.'
    repo_git merge-base --is-ancestor "$cached" "$remote" ||
        die 'Remote main diverged or was rewound relative to cached origin/main.'
    repo_git merge-base --is-ancestor "$head" "$remote" ||
        die 'Current HEAD is ahead of or diverged from remote main.'
    if [[ $remote != "$cached" ]]; then
        repo_git update-ref refs/remotes/origin/main "$remote" "$cached" ||
            die 'origin/main changed during the check.'
    fi
    log "Fetched origin/main $remote."
fi

if [[ $mode == apply ]]; then
    target=$(repo_git rev-parse --disambiguate="$requested") || die 'Cannot resolve requested SHA.'
    [[ $target =~ ^[0-9a-f]{40}$ ]] || die 'Requested SHA is missing or ambiguous.'
    [[ $(repo_git cat-file -t "$target") == commit ]] || die 'Requested SHA is not a commit.'
    repo_git merge-base --is-ancestor "$target" "$remote" || die 'Target is outside trusted origin/main history.'
else
    target=$remote
fi
repo_git merge-base --is-ancestor "$head" "$target" || die 'Target is not a fast-forward from HEAD.'
log "Target $target."
if [[ $head == "$target" ]]; then
    log 'HEAD is already at the target.'
else
    log 'Incoming commit IDs (up to 20):'
    repo_git log --format='%h' -20 "$head..$target"
    repo_git diff --no-ext-diff --shortstat "$head" "$target"
fi

protected_target=$(repo_git ls-tree -r --name-only "$target" -- "${PROTECTED[@]}")
[[ -z $protected_target ]] || die 'Target tracks a protected runtime path.'
for directory in bot napcat; do
    [[ $(repo_git cat-file -t "$target:$directory") == tree ]] || die "Target replaces $directory."
done

# Check new paths, including ignored files and symlink ancestors, before any stop.
check_new_paths() {
    local path parent
    while IFS= read -r -d '' path; do
        [[ ! -e $path && ! -L $path ]] || return 1
        parent=$path
        while [[ $parent == */* ]]; do
            parent=${parent%/*}
            [[ ! -L $parent && ( ! -e $parent || -d $parent ) ]] || return 1
        done
    done
}
repo_git diff --no-renames --diff-filter=A --name-only -z "$head" "$target" |
    check_new_paths || die 'An incoming path conflicts with an existing local path.'

changed() {
    local result
    if repo_git diff --quiet "$head" "$target" -- "$@"; then return 1; else result=$?; fi
    (( result == 1 )) || die 'Unable to compare target changes.'
    return 0
}
deps=0 migration=0 units_changed=0 compose_changed=0 tool_changed=0 update_api=0 update_bot=0
if changed requirements.txt requirements.in; then deps=1; fi
if changed bot/database.py bot/migrate.py bot/migrations; then migration=1; fi
if changed deploy/systemd; then units_changed=1; fi
if changed deploy/update.sh; then tool_changed=1; fi
if changed napcat ':(exclude)napcat/.env.example'; then compose_changed=1; fi
if changed bot/mc_api.py; then update_api=1; fi
if changed bot/bot.py bot/binding_service.py bot/notify_client.py bot/plugins; then update_bot=1; fi
if (( deps || migration )) || changed bot ':(exclude)bot/.env.example' ':(exclude)bot/mc_api.py' \
    ':(exclude)bot/bot.py' ':(exclude)bot/binding_service.py' ':(exclude)bot/notify_client.py' \
    ':(exclude)bot/plugins'; then
    update_api=1 update_bot=1
fi
log "Changes (1=yes): dependencies=$deps migration=$migration systemd=$units_changed napcat=$compose_changed updater=$tool_changed."
log "Planned service operations (1=yes): API=$update_api Bot=$update_bot."
log 'Review target commit and CI manually before Apply. Service changes require a maintenance window.'

active() { systemctl is-active --quiet "$1"; }
napcat_status() {
    timeout 5s docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
        mc-qq-napcat 2>/dev/null
}
for unit in "$API" "$BOT"; do
    if active "$unit"; then log "$unit active"; else log "$unit not active"; fi
done
napcat=$(napcat_status) || napcat=unavailable
case "$napcat" in
    'running healthy'|'running none') log "NapCat $napcat." ;;
    *) log 'NapCat is unavailable, stopped or not healthy.' ;;
esac
if (( units_changed || compose_changed || tool_changed )); then
    log 'Manual maintenance required for systemd, NapCat or updater changes.'
fi
[[ $mode == apply ]] || exit 0
(( ! units_changed && ! compose_changed && ! tool_changed )) ||
    die 'Apply does not install unit changes, modify NapCat or replace the running updater.'
[[ $head != "$target" ]] || die 'HEAD is already at the target. No upgrade ran. After a failed run, follow manual recovery.'

for required in bot/bot.py bot/mc_api.py bot/database.py bot/migrate.py requirements.txt; do
    entry=$(repo_git ls-tree "$target" -- "$required") || die "Cannot inspect target file: $required"
    [[ $entry =~ ^100(644|755)[[:space:]]blob[[:space:]] ]] || die "Missing or non-regular target file: $required"
done
target_entries=$(repo_git ls-tree -r "$target" -- bot napcat) || die 'Cannot inspect target runtime trees.'
if [[ $target_entries =~ (^|$'\n')(120000|160000)\  ]]; then
    die 'Target runtime tree contains a symbolic link or submodule.'
fi

api_ready() {
    local pid
    active "$API" || return 1
    pid=$(systemctl show -p MainPID --value "$API") || return 1
    [[ $pid =~ ^[1-9][0-9]*$ ]] || return 1
    ss -H -ltnp 'sport = :18765' |
        awk -v pid="$pid" '$4 != "127.0.0.1:18765" || index($0,"pid=" pid ",")==0 {bad=1} END {exit !(NR==1 && !bad)}' ||
        return 1
    [[ $(curl -q --noproxy '*' -s --max-time 3 -o /dev/null -w '%{http_code}' \
        http://127.0.0.1:18765/internal/health) == 200 ]] &&
    [[ $(curl -q --noproxy '*' -s --max-time 3 -o /dev/null -w '%{http_code}' \
        http://127.0.0.1:18765/internal/mc/snapshot) == 401 ]]
}
bot_ready() {
    local pid invocation
    active "$BOT" || return 1
    pid=$(systemctl show -p MainPID --value "$BOT") || return 1
    invocation=$(systemctl show -p InvocationID --value "$BOT") || return 1
    [[ $pid =~ ^[1-9][0-9]*$ && $invocation =~ ^[0-9a-f]{32}$ ]] || return 1
    ss -H -tnp |
        awk -v pid="$pid" '$1=="ESTAB" && $5=="127.0.0.1:13001" && index($0,"pid=" pid ",") {ok=1} END {exit !ok}' ||
        return 1
    timeout 4s journalctl --no-pager -q -o cat -u "$BOT" "_SYSTEMD_INVOCATION_ID=$invocation" 2>/dev/null |
        awk '/Bot [0-9]+ disconnected/ {ok=0} /Bot [0-9]+ connected/ {ok=1} END {exit !ok}'
}
wait_ready() {
    local attempt
    for ((attempt=1; attempt<=15; attempt++)); do
        if "$1"; then return 0; fi
        sleep 2
    done
    return 1
}

db_version() {
    local version
    [[ -f $1 && -s $1 && ! -L $1 ]] || return 1
    version=$(sqlite3 -readonly -batch -bail "$1" 'PRAGMA user_version;' 2>/dev/null) || return 1
    [[ $version =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$version"
}
db_checked_version() {
    local result
    result=$(sqlite3 -readonly -batch -bail "$1" 'PRAGMA quick_check;' 2>/dev/null) || return 1
    [[ $result == ok ]] || return 1
    db_version "$1"
}
backup_database() {
    local kind=$1 version saved_version
    if [[ ! -e $BACKUPS && ! -L $BACKUPS ]]; then
        install -d -m 0700 -- "$BACKUPS" || die 'Could not create backup directory.'
    fi
    [[ -d $BACKUPS && ! -L $BACKUPS && $(stat -c '%u:%a' -- "$BACKUPS") == 0:700 ]] ||
        die 'Backup directory must be a real root-owned directory with mode 700.'
    version=$(db_version "$DB") || die 'Unable to read database version.'
    incomplete_backup=$(mktemp "$BACKUPS/auth-$kind-$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX.db") ||
        die 'Could not reserve a unique backup file.'
    sqlite3 -readonly -batch -bail "$DB" ".backup '$incomplete_backup'" >/dev/null 2>&1 ||
        die 'SQLite backup failed.'
    [[ -s $incomplete_backup && $(stat -c '%u:%a' -- "$incomplete_backup") == 0:600 ]] ||
        die 'Backup is empty or does not have root ownership and mode 600.'
    saved_version=$(db_checked_version "$incomplete_backup") || die 'Backup quick_check or schema version failed.'
    [[ $saved_version == "$version" ]] || die 'Backup schema version differs from source.'
    verified_backup=$incomplete_backup
    incomplete_backup=
    log "Verified backup (schema $saved_version): $verified_backup"
}
pip_locked() {
    PIP_CONFIG_FILE=/dev/null "$PYTHON" -I -m pip --isolated --disable-pip-version-check --no-cache-dir "$@"
}

if (( update_api || update_bot )); then
    [[ $napcat == 'running healthy' || $napcat == 'running none' ]] || die 'NapCat baseline is not running.'
    api_ready && bot_ready || die 'API or OneBot baseline health is unverified; review locally.'
fi
if (( deps )); then
    phase=wheel-download
    scratch=$(mktemp -d /tmp/mc-qq-auth-update.XXXXXXXX) || die 'Cannot create private download directory.'
    repo_git show "$target:requirements.txt" >"$scratch/requirements.txt" || die 'Cannot read target lock file.'
    # Accept only pinned package entries and the platform marker used by this project.
    awk 'BEGIN { quote=sprintf("%c",34); marker="; sys_platform == " quote "win32" quote "$" }
        /^[[:space:]]*(#|$)/ {next}
        {line=$0; sub(marker,"",line); if (line !~ /^[a-zA-Z0-9][a-zA-Z0-9._-]*(\[[a-zA-Z0-9._,-]+\])?==[0-9][a-zA-Z0-9.!+-]*$/) bad=1; n++}
        END {exit !(n && !bad)}' "$scratch/requirements.txt" || die 'Target lock contains unreviewed directives or unpinned packages.'
    mkdir "$scratch/wheels"
    pip_locked download --index-url https://pypi.org/simple --only-binary=:all: \
        --dest "$scratch/wheels" -r "$scratch/requirements.txt" >/dev/null 2>&1 ||
        die 'Dependency wheel download failed.'
    log 'Target dependency wheels downloaded before stopping services.'
fi

if (( update_api || update_bot )); then
    phase=backup
    backup_database online
fi
phase=preflight
clean_worktree
[[ $(repo_git rev-parse HEAD) == "$head" && $(repo_git symbolic-ref --short HEAD) == main ]] ||
    die 'HEAD or branch changed during preflight.'
phase=stop
if (( update_bot )); then
    systemctl stop "$BOT" >/dev/null 2>&1 || die 'Failed to stop Bot.'
    log "Stopped $BOT."
fi
if (( update_api )); then
    systemctl stop "$API" >/dev/null 2>&1 || die 'Failed to stop API.'
    log "Stopped $API."
fi
if (( migration )); then
    phase=offline-backup
    for unit in "$BOT" "$API"; do
        [[ $(systemctl show -p ActiveState --value "$unit") == inactive ]] || die "Unit is not stopped: $unit"
    done
    backup_database stopped
fi

phase=git-update
repo_git merge --ff-only --no-autostash --no-overwrite-ignore "$target" >/dev/null 2>&1 ||
    die 'Fast-forward failed. Inspect the repository locally.'
[[ $(repo_git rev-parse HEAD) == "$target" ]] || die 'HEAD does not match requested target.'
log "Updated source to $target."
if (( deps )); then
    phase=dependencies
    pip_locked install --no-index --find-links "$scratch/wheels" --only-binary=:all: \
        -r "$scratch/requirements.txt" >/dev/null 2>&1 && pip_locked check >/dev/null 2>&1 ||
        die 'Dependency installation or pip check failed.'
    log 'Locked dependencies installed and checked.'
fi
if (( migration )); then
    phase=migration
    for unit in "$BOT" "$API"; do
        [[ $(systemctl show -p ActiveState --value "$unit") == inactive ]] || die "Unit is not stopped: $unit"
    done
    "$PYTHON" -B bot/migrate.py >/dev/null 2>&1 || die 'Project SQLite migration failed.'
    version=$(db_checked_version "$DB") || die 'Post-migration quick_check or schema version failed.'
    log "Project migration finished. Verified SQLite schema $version."
fi

if (( update_api || update_bot )); then
    phase=restart
    if (( update_api )); then
        attempted_starts+=("$API")
        systemctl start "$API" >/dev/null 2>&1 || die 'Failed to start API.'
        log "Started $API."
    fi
    phase=health
    wait_ready api_ready || die 'API PID, loopback listener, health 200 or snapshot 401 did not verify.'
    log 'API health verified on loopback, including snapshot HTTP 401 without credentials.'
    if (( update_bot )); then
        phase=restart
        attempted_starts+=("$BOT")
        systemctl start "$BOT" >/dev/null 2>&1 || die 'Failed to start Bot.'
        log "Started $BOT."
    fi
    phase=health
    wait_ready bot_ready || die 'Bot OneBot connection did not verify. Check journald locally.'
    api_ready || die 'API health changed after checking Bot.'
    napcat=$(napcat_status) || die 'NapCat status became unavailable.'
    [[ $napcat == 'running healthy' || $napcat == 'running none' ]] || die 'NapCat is not running or healthy.'
    log 'OneBot connection and both services verified.'
else
    log 'Source-only update. No service, dependency or database operations were needed.'
fi
log "Upgrade finished at $target."
