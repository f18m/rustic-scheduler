# Secrets

Copy each `*.example` file to the same name without `.example`, fill it in and
restrict it to the user the container runs as:

```sh
for f in *.example; do cp -n "$f" "${f%.example}"; done
chmod 600 rustic_password aws_credentials mqtt_password apprise.conf
sudo chown 1000:1000 rustic_password aws_credentials mqtt_password apprise.conf
```

Everything in this directory except the examples is ignored by Git.
