from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from .models import EmployeeProfile


class WorkspaceAuthenticationForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={"autofocus": True, "placeholder": "Username"}))
    password = forms.CharField(strip=False, widget=forms.PasswordInput(attrs={"placeholder": "Password"}))
    remember_me = forms.BooleanField(required=False, initial=True)

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        profile = getattr(user, "employee_profile", None)
        if getattr(profile, "account_type", "") != EmployeeProfile.AccountType.EXTERNAL_VENDOR:
            return
        commercial = getattr(user, "vendor_commercial_profile", None)
        if not commercial or not commercial.is_active or not commercial.panel_access_enabled:
            raise ValidationError(
                "Panel access is not enabled for this supplier account.",
                code="panel_access_disabled",
            )


class FirstAdminSetupForm(forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    username = forms.CharField(max_length=150)
    email = forms.EmailField()
    password1 = forms.CharField(min_length=8, strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(min_length=8, strip=False, widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username is already in use.")
        return username

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") and cleaned.get("password1") != cleaned.get("password2"):
            self.add_error("password2", "Passwords do not match.")
        return cleaned

