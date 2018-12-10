"""Core views"""
from logging import getLogger
from typing import Dict

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, reverse
from django.utils.translation import ugettext as _
from django.views import View
from django.views.generic import FormView

from passbook.core.forms.authentication import LoginForm, SignUpForm
from passbook.core.models import Invite, User
from passbook.core.signals import invite_used, user_signed_up
from passbook.lib.config import CONFIG

LOGGER = getLogger(__name__)

class LoginView(UserPassesTestMixin, FormView):
    """Allow users to sign in"""

    template_name = 'login/form.html'
    form_class = LoginForm
    success_url = '.'

    # Allow only not authenticated users to login
    def test_func(self):
        return self.request.user.is_authenticated is False

    def handle_no_permission(self):
        return self.logged_in_redirect()

    def logged_in_redirect(self):
        """User failed check so user is authenticated already.
        Either redirect to ?next param or home."""
        if 'next' in self.request.GET:
            return redirect(self.request.GET.get('next'))
        return redirect(reverse('passbook_core:overview'))

    def get_context_data(self, **kwargs):
        kwargs['config'] = CONFIG.get('passbook')
        kwargs['is_login'] = True
        kwargs['title'] = _('Log in to your account')
        kwargs['primary_action'] = _('Log in')
        kwargs['show_sign_up_notice'] = CONFIG.y('passbook.sign_up.enabled')
        kwargs['show_password_forget_notice'] = CONFIG.y('passbook.password_reset.enabled')
        return super().get_context_data(**kwargs)

    def get_user(self, uid_value) -> User:
        """Find user instance. Returns None if no user was found."""
        for search_field in CONFIG.y('passbook.uid_fields'):
            users = User.objects.filter(**{search_field: uid_value})
            if users.exists():
                return users.first()
        return None

    def form_valid(self, form: LoginForm) -> HttpResponse:
        """Form data is valid"""
        pre_user = self.get_user(form.cleaned_data.get('uid_field'))
        if not pre_user:
            # No user found
            return self.invalid_login(self.request)
        user = authenticate(
            email=pre_user.email,
            username=pre_user.username,
            password=form.cleaned_data.get('password'),
            request=self.request)
        if user:
            # User authenticated successfully
            return self.login(self.request, user, form.cleaned_data)
        # User was found but couldn't authenticate
        return self.invalid_login(self.request, disabled_user=pre_user)

    def login(self, request: HttpRequest, user: User, cleaned_data: Dict) -> HttpResponse:
        """Handle actual login

        Actually logs user in, sets session expiry and redirects to ?next parameter

        Args:
            request: The current request
            user: The user to be logged in.

        Returns:
            Either redirect to ?next or if not present to overview
        """
        if user is None:
            raise ValueError("User cannot be None")
        login(request, user)

        if cleaned_data.get('remember') is True:
            request.session.set_expiry(CONFIG.y('passbook.session.remember_age'))
        else:
            request.session.set_expiry(0)  # Expires when browser is closed
        messages.success(request, _("Successfully logged in!"))
        LOGGER.debug("Successfully logged in %s", user.username)
        return self.logged_in_redirect()

    def invalid_login(self, request: HttpRequest, disabled_user: User = None) -> HttpResponse:
        """Handle login for disabled users/invalid login attempts"""
        messages.error(request, _('Failed to authenticate.'))
        return self.render_to_response(self.get_context_data())

class LogoutView(LoginRequiredMixin, View):
    """Log current user out"""

    def dispatch(self, request):
        """Log current user out"""
        logout(request)
        messages.success(request, _("You've successfully been logged out."))
        return redirect(reverse('passbook_core:auth-login'))


class SignUpView(UserPassesTestMixin, FormView):
    """Sign up new user, optionally consume one-use invite link."""

    template_name = 'login/form.html'
    form_class = SignUpForm
    success_url = '.'
    # Invite insatnce, if invite link was used
    _invite = None
    # Instance of newly created user
    _user = None

    # Allow only not authenticated users to login
    def test_func(self):
        return self.request.user.is_authenticated is False

    def handle_no_permission(self):
        return redirect(reverse('passbook_core:overview'))

    def dispatch(self, request, *args, **kwargs):
        """Check if sign-up is enabled or invite link given"""
        allowed = False
        if 'invite' in request.GET:
            invites = Invite.objects.filter(uuid=request.GET.get('invite'))
            allowed = invites.exists()
            if allowed:
                self._invite = invites.first()
        if CONFIG.y('passbook.sign_up.enabled'):
            allowed = True
        if not allowed:
            messages.error(request, _('Sign-ups are currently disabled.'))
            return redirect(reverse('passbook_core:auth-login'))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        kwargs['config'] = CONFIG.get('passbook')
        kwargs['is_login'] = True
        kwargs['title'] = _('Sign Up')
        kwargs['primary_action'] = _('Sign up')
        return super().get_context_data(**kwargs)

    def form_valid(self, form: SignUpForm) -> HttpResponse:
        """Create user"""
        self._user = SignUpView.create_user(form.cleaned_data, self.request)
        self.consume_invite()
        messages.success(self.request, _("Successfully signed up!"))
        LOGGER.debug("Successfully signed up %s",
                     form.cleaned_data.get('email'))
        return redirect(reverse('passbook_core:auth-login'))

    def consume_invite(self):
        """Consume invite if an invite was used"""
        if self._invite:
            invite_used.send(
                sender=self,
                request=self.request,
                invite=self._invite,
                user=self._user)
            self._invite.delete()

    @staticmethod
    def create_user(data: Dict, request: HttpRequest = None) -> User:
        """Create user from data

        Args:
            data: Dictionary as returned by SignupForm's cleaned_data
            request: Optional current request.

        Returns:
            The user created

        Raises:
            SignalException: if any signals raise an exception. This also deletes the created user.
        """
        # Create user
        new_user = User.objects.create_user(
            username=data.get('username'),
            email=data.get('email'),
            first_name=data.get('first_name'),
            last_name=data.get('last_name'),
        )
        new_user.is_active = True
        new_user.set_password(data.get('password'))
        new_user.save()
        # Send signal for other auth sources
        user_signed_up.send(
            sender=SignUpView,
            user=new_user,
            request=request)
        # try:
            # TODO: Create signal for signup
            # on_user_sign_up.send(
            #     sender=None,
            #     user=new_user,
            #     request=request,
            #     password=data.get('password'),
            #     needs_confirmation=needs_confirmation)
            # TODO: Implement Verification, via email or others
            # if needs_confirmation:
                # Create Account Confirmation UUID
                # AccountConfirmation.objects.create(user=new_user)
        # except SignalException as exception:
        #     LOGGER.warning("Failed to sign up user %s", exception, exc_info=exception)
        #     new_user.delete()
        #     raise
        return new_user
