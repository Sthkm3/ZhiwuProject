from app import config, mailer


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.sent, self.tls = host, port, [], False
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        self.login_as = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _setup(monkeypatch, port):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_PORT", port)
    monkeypatch.setattr(config, "SMTP_USER", "me@gmail.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(config, "MAIL_FROM", "noreply@zhiwu-project.sthkm3.org")


def test_sends_from_noreply(monkeypatch):
    _setup(monkeypatch, 465)
    mailer.send_code("user@example.com", "123456", "ja")
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port, smtp.tls) == ("smtp.example.com", 465, False)
    msg = smtp.sent[0]
    assert msg["From"] == "吱鼯計畫 <noreply@zhiwu-project.sthkm3.org>"
    assert msg["To"] == "user@example.com"
    assert "123456" in msg["Subject"] and "認証コード" in msg["Subject"]
    assert msg["Message-ID"].endswith("@zhiwu-project.sthkm3.org>")


def test_starttls_on_587(monkeypatch):
    _setup(monkeypatch, 587)
    mailer.send_code("user@example.com", "123456", "en")
    assert FakeSMTP.instances[0].tls is True


def test_dev_mode_prints(monkeypatch, capsys):
    monkeypatch.setattr(config, "SMTP_USER", "")
    mailer.send_code("user@example.com", "654321", "en")
    assert "654321" in capsys.readouterr().out
