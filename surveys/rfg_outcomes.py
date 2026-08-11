from .models import SurveyAttempt
from .rfg_text import clean_rfg_display_text


RFG_RESULT_DETAILS = {
    "1": (SurveyAttempt.Status.COMPLETED, "Survey completed", "Your response was completed successfully.", "success"),
    "2": (SurveyAttempt.Status.TERMINATED, "Survey terminated", "The survey ended because the respondent did not meet its requirements.", "warning"),
    "3": (SurveyAttempt.Status.OVER_QUOTA, "Quota full", "The matching survey quota was already full.", "warning"),
    "4": (SurveyAttempt.Status.TERMINATED, "Survey ended", "The provider ended this survey attempt.", "warning"),
    "5": (SurveyAttempt.Status.TERMINATED, "Survey closed", "The survey closed before this attempt could be completed.", "warning"),
    "7": (SurveyAttempt.Status.TERMINATED, "Early termination", "The respondent did not match the survey's targeting criteria.", "warning"),
    "8": (SurveyAttempt.Status.TERMINATED, "Duplicate respondent", "This respondent was identified as a previous attempt for this survey or survey group.", "warning"),
    "9": (SurveyAttempt.Status.OVER_QUOTA, "Quota full", "A matching quota filled during pre-screening.", "warning"),
    "10": (SurveyAttempt.Status.QUALITY_TERMINATED, "Security termination", "The client rejected this attempt for a security or quality reason.", "danger"),
    "11": (SurveyAttempt.Status.TERMINATED, "No survey found", "No eligible survey was available for this respondent.", "warning"),
    "12": (SurveyAttempt.Status.TERMINATED, "All surveys attempted", "All eligible surveys had already been attempted.", "warning"),
    "13": (SurveyAttempt.Status.TERMINATED, "Maximum attempts reached", "The maximum number of routing attempts was reached.", "warning"),
    "19": (SurveyAttempt.Status.TERMINATED, "Survey paused", "The survey is temporarily paused.", "warning"),
    "30": (SurveyAttempt.Status.QUALITY_TERMINATED, "Fraud prevention", "The attempt was rejected by MaxMind fraud screening.", "danger"),
    "31": (SurveyAttempt.Status.QUALITY_TERMINATED, "Invalid IP address", "The respondent IP address was invalid.", "danger"),
    "32": (SurveyAttempt.Status.QUALITY_TERMINATED, "Blocked IP address", "The respondent IP address was found on a blocklist.", "danger"),
    "33": (SurveyAttempt.Status.QUALITY_TERMINATED, "Postal-code distance mismatch", "The respondent location was too far from the submitted postal code.", "danger"),
    "34": (SurveyAttempt.Status.QUALITY_TERMINATED, "Blocked respondent", "The respondent is blocked by provider security rules.", "danger"),
    "35": (SurveyAttempt.Status.QUALITY_TERMINATED, "Country changed", "The respondent country changed during the survey journey.", "danger"),
    "36": (SurveyAttempt.Status.QUALITY_TERMINATED, "Country mismatch", "The respondent IP country did not match the submitted country.", "danger"),
    "37": (SurveyAttempt.Status.QUALITY_TERMINATED, "Invalid date of birth", "The submitted date of birth was invalid.", "danger"),
    "38": (SurveyAttempt.Status.QUALITY_TERMINATED, "Minimum age not met", "The respondent was below the provider's minimum age.", "danger"),
    "39": (SurveyAttempt.Status.QUALITY_TERMINATED, "Age mismatch", "The submitted age did not match provider profile data.", "danger"),
    "40": (SurveyAttempt.Status.QUALITY_TERMINATED, "Invalid gender", "The submitted gender value was invalid.", "danger"),
    "41": (SurveyAttempt.Status.QUALITY_TERMINATED, "Invalid postal code", "The submitted postal code was invalid for the selected country.", "danger"),
    "42": (SurveyAttempt.Status.QUALITY_TERMINATED, "Unsupported device", "The respondent operating system was not allowed.", "danger"),
    "43": (SurveyAttempt.Status.QUALITY_TERMINATED, "Provider verification failed", "The respondent did not pass the provider verification test.", "danger"),
    "44": (SurveyAttempt.Status.QUALITY_TERMINATED, "Robot check failed", "The respondent did not pass the robot check.", "danger"),
    "50": (SurveyAttempt.Status.QUALITY_TERMINATED, "Speeding detected", "The survey was completed faster than the permitted quality threshold.", "danger"),
    "51": (SurveyAttempt.Status.QUALITY_TERMINATED, "Invalid callback hash", "The client callback hash could not be verified.", "danger"),
    "52": (SurveyAttempt.Status.QUALITY_TERMINATED, "Client IP verification failed", "The client callback IP was not on the required allowlist.", "danger"),
    "53": (SurveyAttempt.Status.QUALITY_TERMINATED, "Client verification failed", "The respondent did not pass the client's verification test.", "danger"),
}

RFG_STATUS_MAP = {code: detail[0] for code, detail in RFG_RESULT_DETAILS.items()}

RFG_LIVE_SECURITY = {
    "1": "Blocked panelist",
    "2": "Suspicious or proxy IP address",
    "3": "Invalid IP address",
    "4": "IP country does not match the submitted country",
}

RFG_LIVE_INVALID = {
    "1": "Invalid date of birth",
    "2": "Invalid gender",
    "3": "Invalid country",
    "4": "Invalid postal code",
    "5": "IP location does not match the submitted country",
}


def result_code_for_attempt(attempt):
    return {
        SurveyAttempt.Status.COMPLETED: "1",
        SurveyAttempt.Status.TERMINATED: "2",
        SurveyAttempt.Status.OVER_QUOTA: "3",
        SurveyAttempt.Status.QUALITY_TERMINATED: "10",
    }.get(attempt.status, "")


def describe_rfg_outcome(parameters, attempt=None):
    parameters = parameters or {}
    code = str(parameters.get("result") or (result_code_for_attempt(attempt) if attempt else ""))
    status, title, message, tone = RFG_RESULT_DETAILS.get(
        code,
        (SurveyAttempt.Status.REDIRECTED, "Result received", "The provider result is awaiting confirmation.", "info"),
    )
    reason = str(parameters.get("ruledOutBy") or parameters.get("reason") or "").strip()
    live_invalid = RFG_LIVE_INVALID.get(str(parameters.get("liveI") or ""))
    live_security = RFG_LIVE_SECURITY.get(str(parameters.get("liveS") or ""))
    local_reason = str(parameters.get("local_reason") or "").strip()
    reason = local_reason or reason or live_invalid or live_security or message
    if str(parameters.get("quotaThrottle") or "") == "1":
        reason = "The matching quota is temporarily throttled. It may reopen after approximately 10 minutes."
    reason = clean_rfg_display_text(reason)
    return {
        "code": code,
        "status": status,
        "title": title,
        "message": message,
        "reason": reason,
        "tone": tone,
        "sesskey": str(parameters.get("sesskey") or ""),
    }
