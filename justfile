# Dev loop: Mac -> pepsi (Bazzite HTPC). `just key` once ends password friction.

host := "pepsi"
remote_dir := "~/dev/lgtv-companion-linux"
venv := "/usr/local/lib/lgtv-companion"

# Install the Mac's SSH key on the box (asks for the password one last time)
key:
    ssh-copy-id -i ~/.ssh/id_ed25519_pepsi.pub -o PubkeyAuthentication=no {{host}}

# rsync the source over and reinstall into the box venv, restart the daemon.
# NOT an editable install: SELinux blocks system services from reading
# user_home_t, so the code must live inside the venv.
deploy:
    rsync -az --delete --exclude .venv --exclude .git --exclude __pycache__ \
        ./ {{host}}:{{remote_dir}}/
    ssh {{host}} "sudo {{venv}}/bin/pip install -q --no-deps --force-reinstall {{remote_dir}} && \
        sudo systemctl try-restart lgtvc-daemon 2>/dev/null; true"

# First-time box setup: create the venv and install with deps
bootstrap:
    rsync -az --delete --exclude .venv --exclude .git --exclude __pycache__ \
        ./ {{host}}:{{remote_dir}}/
    ssh {{host}} "sudo python3 -m venv {{venv}} && \
        sudo {{venv}}/bin/pip install -q {{remote_dir}} && \
        sudo ln -sf {{venv}}/bin/lgtvc {{venv}}/bin/lgtvc-daemon /usr/local/bin/"

logs:
    ssh {{host}} "journalctl -u lgtvc-daemon -f -o cat"

status:
    ssh {{host}} "systemctl status lgtvc-daemon lgtvc-shutdown --no-pager; \
        systemctl is-enabled lgtv-startup lgtv-shutdown lgtv-sleep 2>&1"

# Run any lgtvc command on the box: just tv -- -poweron
tv *ARGS:
    ssh {{host}} "lgtvc {{ARGS}}"

test:
    .venv/bin/pytest -q

e2e:
    ssh {{host}} "bash {{remote_dir}}/tests/e2e/basic.sh"
