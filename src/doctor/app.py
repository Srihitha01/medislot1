import os
import secrets
from datetime import timedelta
from functools import wraps

import requests
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix


# --------------------------------------------------
# MediSlot API configuration
# --------------------------------------------------

API_BASE_URL = os.environ.get(
    "MEDISLOT_API_BASE_URL",
    "https://vmfftty8x3.execute-api.ap-south-1.amazonaws.com/Stage",
).rstrip("/")

# Change this environment variable if your API Gateway
# login route is different.
LOGIN_PATH = os.environ.get("MEDISLOT_LOGIN_PATH", "/login")


# --------------------------------------------------
# Flask application
# --------------------------------------------------

app = Flask(__name__)

app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
)

# Persistent Flask signing key
SECRET_FILE = os.environ.get(
    "FLASK_SECRET_FILE",
    "/opt/medislot-dashboard/.flask_secret",
)

secret_directory = os.path.dirname(SECRET_FILE)
os.makedirs(secret_directory, exist_ok=True)

if os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "r", encoding="utf-8") as file:
        flask_secret = file.read().strip()
else:
    flask_secret = secrets.token_urlsafe(48)

    file_descriptor = os.open(
        SECRET_FILE,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )

    with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
        file.write(flask_secret)

app.secret_key = flask_secret

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(
        os.environ.get("COOKIE_SECURE", "false").lower() == "true"
    ),
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=15),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)


# --------------------------------------------------
# API helper
# --------------------------------------------------

def api_url(path):
    return f"{API_BASE_URL}/{path.lstrip('/')}"


# --------------------------------------------------
# Doctor authentication decorator
# --------------------------------------------------

def doctor_required(function):
    @wraps(function)
    def wrapped(*args, **kwargs):

        if not session.get("access_token"):
            return redirect(url_for("login"))

        if session.get("role") != "doctor":
            session.clear()
            flash("Doctor access is required.", "error")
            return redirect(url_for("login"))

        return function(*args, **kwargs)

    return wrapped


# --------------------------------------------------
# Security headers
# --------------------------------------------------

@app.after_request
def security_headers(response):

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = (
        "strict-origin-when-cross-origin"
    )
    response.headers["Cache-Control"] = "no-store"

    return response


# --------------------------------------------------
# Health check
# --------------------------------------------------

@app.get("/health")
def health():

    return {
        "status": "healthy",
        "service": "medislot-doctor-dashboard",
    }, 200


# --------------------------------------------------
# Doctor login
# --------------------------------------------------

@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():

    if (
        session.get("access_token")
        and session.get("role") == "doctor"
    ):
        return redirect(url_for("dashboard"))

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash(
                "Please enter your email and password.",
                "error",
            )
            return render_template("login.html"), 400

        try:
            response = requests.post(
                api_url(LOGIN_PATH),
                json={
                    "email": email,
                    "password": password,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=(5, 20),
            )

            try:
                result = response.json()
            except ValueError:
                result = {}

            if response.status_code != 200:

                message = result.get(
                    "message",
                    "Login failed. Please check your credentials.",
                )

                flash(message, "error")

                return (
                    render_template("login.html"),
                    502 if response.status_code >= 500
                    else response.status_code,
                )

            if result.get("role") != "doctor":
                flash(
                    "This account is not registered as a doctor.",
                    "error",
                )
                return render_template("login.html"), 403

            access_token = result.get("accessToken")

            if not access_token:
                flash(
                    "Login service did not return an access token.",
                    "error",
                )
                return render_template("login.html"), 502

            # Start a fresh authenticated session
            session.clear()
            session.permanent = True

            session["access_token"] = access_token
            session["role"] = result["role"]
            session["user_id"] = str(result.get("userId", ""))
            session["email"] = email

            return redirect(url_for("dashboard"))

        except requests.RequestException:
            app.logger.exception(
                "MediSlot login API request failed"
            )

            flash(
                "Cannot reach the MediSlot login service. Please try again.",
                "error",
            )

            return render_template("login.html"), 502

    return render_template("login.html")


# --------------------------------------------------
# Doctor dashboard
# --------------------------------------------------

@app.get("/dashboard")
@doctor_required
def dashboard():

    return render_template(
        "doctor_dashboard.html",
        api_base_url=API_BASE_URL,
        access_token=session["access_token"],
    )


# --------------------------------------------------
# Logout
# --------------------------------------------------

@app.post("/logout")
def logout():

    session.clear()

    flash(
        "You have been logged out.",
        "success",
    )

    return redirect(url_for("login"))


# --------------------------------------------------
# 404 handler
# --------------------------------------------------

@app.errorhandler(404)
def not_found(error):

    return redirect(url_for("login"))


# --------------------------------------------------
# Local development entry point
# --------------------------------------------------

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=8000,
        debug=False,
    )