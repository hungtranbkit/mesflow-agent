# MESFlow Deploy Agent local installer

This package installs only the loopback-bound Docker Deploy Agent. It preserves
`/opt/mesflow/.env` and MESFlow runtime data; it does not deploy MESFlow, modify
nginx/firewall/systemd, or change PostgreSQL. Review and run `./install.sh` only
on an approved local Ubuntu host.
