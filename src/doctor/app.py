import os
import re
import secrets
from datetime import timedelta
from functools import wraps

import requests
from flask import (
    Flask,
    flash,
    jsonify,
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

API_BASE_URL = os.environ.get("MEDISLOT_API_BASE_URL", "").rstrip("/")
if not API_BASE_URL:
    raise RuntimeError("MEDISLOT_API_BASE_URL environment variable is required")

LOGIN_PATH = os.environ.get("MEDISLOT_LOGIN_PATH", "/auth/login")

# Change these if your API Gateway register routes are different.
REGISTER_DOCTOR_PATH = os.environ.get(
    "MEDISLOT_REGISTER_DOCTOR_PATH", "/auth/register"
)
REGISTER_PATIENT_PATH = os.environ.get(
    "MEDISLOT_REGISTER_PATIENT_PATH", "/auth/register"
)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_PATTERN = re.compile(r"^\+?[0-9\s\-]{7,15}$")


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

flask_secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
if not flask_secret:
    # Local development fallback only; EC2 provides FLASK_SECRET_KEY.
    flask_secret = secrets.token_urlsafe(48)

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
# API helpers
# --------------------------------------------------

def api_url(path):
    return f"{API_BASE_URL}/{path.lstrip('/')}"


def load_reference(path):
    """Fetch dropdown data. Returns [] if the API needs a token or fails."""
    try:
        response = requests.get(
            api_url(path),
            headers={"Accept": "application/json"},
            timeout=(5, 10),
        )
        if response.status_code == 200:
            return response.json().get("items", [])
    except (requests.RequestException, ValueError):
        app.logger.warning("Could not load reference data: %s", path)
    return []


def post_registration(path, payload):
    """
    Send a registration request to the MediSlot API.
    Returns (success, message).
    """
    try:
        response = requests.post(
            api_url(path),
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=(5, 20),
        )
    except requests.RequestException:
        app.logger.exception("MediSlot register API request failed")
        return False, "Cannot reach the MediSlot service. Please try again."

    try:
        result = response.json()
    except ValueError:
        result = {}

    if response.status_code in (200, 201):
        return True, result.get(
            "message", "Registration successful. Please log in."
        )

    if response.status_code == 409:
        return False, result.get(
            "message", "An account with this email already exists."
        )

    if response.status_code >= 500:
        return False, "The registration service had a problem. Please try again."

    return False, result.get(
        "message", "Registration failed. Please check your details."
    )


def validate_common(form):
    """Checks shared by doctor and patient registration. Returns error or None."""
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    confirm = form.get("confirm_password", "")

    if not name:
        return "Please enter your full name."
    if not EMAIL_PATTERN.match(email):
        return "Please enter a valid email address."
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if password != confirm:
        return "Passwords do not match."
    return None


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
# Login
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
            flash("Please enter your email and password.", "error")
            return render_template("login.html"), 400

        try:
            response = requests.post(
                api_url(LOGIN_PATH),
                json={"email": email, "password": password},
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

            session.clear()
            session.permanent = True

            session["access_token"] = access_token
            session["refresh_token"] = result.get("refreshToken", "")
            session["role"] = result["role"]
            session["user_id"] = str(result.get("userId", ""))
            session["email"] = email

            return redirect(url_for("dashboard"))

        except requests.RequestException:
            app.logger.exception("MediSlot login API request failed")

            flash(
                "Cannot reach the MediSlot login service. Please try again.",
                "error",
            )

            return render_template("login.html"), 502

    return render_template("login.html")


# --------------------------------------------------
# Registration
# --------------------------------------------------

@app.get("/register")
def register():
    return redirect(url_for("register_patient"))


@app.route("/register/doctor", methods=["GET", "POST"])
def register_doctor():

    specialisations = load_reference("/reference/specialisations")
    hospitals = load_reference("/reference/hospitals")

    def render(status=200):
        return (
            render_template(
                "register.html",
                role="doctor",
                form=request.form,
                specialisations=specialisations,
                hospitals=hospitals,
            ),
            status,
        )

    if request.method == "GET":
        return render()

    error = validate_common(request.form)

    license_number = request.form.get("license_number", "").strip()
    specialisation_id = request.form.get("specialisation_id", "").strip()
    hospital_id = request.form.get("hospital_id", "").strip()
    experience = request.form.get("years_experience", "").strip()

    if not error and not license_number:
        error = "Please enter your medical license number."
    if not error and not specialisation_id.isdigit():
        error = "Please select your specialisation."
    if not error and hospital_id and not hospital_id.isdigit():
        error = "Please select a valid hospital."
    if not error and experience and (
        not experience.isdigit() or int(experience) > 80
    ):
        error = "Years of experience must be between 0 and 80."

    if error:
        flash(error, "error")
        return render(400)

    payload = {
        "role": "doctor",
        "name": request.form["name"].strip(),
        "email": request.form["email"].strip().lower(),
        "password": request.form["password"],
        "licenseNumber": license_number,
        "specialisationId": int(specialisation_id),
        "hospitalId": int(hospital_id) if hospital_id else None,
        "yearsExperience": int(experience) if experience else None,
        "qualifications": request.form.get("qualifications", "").strip(),
        "languages": request.form.get("languages", "").strip(),
        "timezone": "Asia/Kolkata",
    }

    success, message = post_registration(REGISTER_DOCTOR_PATH, payload)

    if not success:
        flash(message, "error")
        return render(400)

    flash(
        f"{message} After logging in, upload your photo from the Profile page.",
        "success",
    )
    return redirect(url_for("login"))


@app.route("/register/patient", methods=["GET", "POST"])
def register_patient():

    def render(status=200):
        return (
            render_template(
                "register.html",
                role="patient",
                form=request.form,
                specialisations=[],
                hospitals=[],
            ),
            status,
        )

    if request.method == "GET":
        return render()

    error = validate_common(request.form)

    phone = request.form.get("phone", "").strip()
    city = request.form.get("city", "").strip()

    if not error and not PHONE_PATTERN.match(phone):
        error = "Please enter a valid phone number."
    if not error and not city:
        error = "Please enter your city."

    if error:
        flash(error, "error")
        return render(400)

    payload = {
        "role": "patient",
        "name": request.form["name"].strip(),
        "email": request.form["email"].strip().lower(),
        "password": request.form["password"],
        "phone": phone,
        "city": city,
    }

    success, message = post_registration(REGISTER_PATIENT_PATH, payload)

    if not success:
        flash(message, "error")
        return render(400)

    flash(
        f"{message} Patients use the MediSlot patient app to book appointments.",
        "success",
    )
    return redirect(url_for("login"))


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

@app.post("/session/refresh")
@doctor_required
def session_refresh():
    refresh_token = session.get("refresh_token")

    if not refresh_token:
        return jsonify({"message": "Session expired"}), 401

    try:
        api_response = requests.post(
            f"{API_BASE_URL}/auth/refresh",
            json={"refreshToken": refresh_token},
            timeout=15,
        )
        result = api_response.json() if api_response.content else {}
    except (requests.RequestException, ValueError):
        app.logger.exception("MediSlot token refresh failed")
        return jsonify({"message": "Could not refresh session"}), 502

    new_access = result.get("accessToken")

    if api_response.status_code >= 400 or not new_access:
        return jsonify({"message": "Session expired"}), 401

    session["access_token"] = new_access
    session["refresh_token"] = result.get("refreshToken") or refresh_token

    return jsonify({"accessToken": new_access})


@app.route("/logout", methods=["GET", "POST"])
def logout():

    session.clear()
    flash("You have been logged out.", "success")

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

    app.run(host="127.0.0.1", port=8000, debug=False)