"""
Authentification par passkeys (WebAuthn) pour Cardinal.

On s'appuie sur py_webauthn. Deux cérémonies :
  - enregistrement (register) : crée une passkey pour un nouveau compte,
  - authentification (login)   : prouve la possession d'une passkey existante.

Les passkeys sont « découvrables » (resident key) → connexion SANS identifiant :
l'utilisateur choisit juste sa passkey, on retrouve le compte via l'ID du credential.

Config via variables d'env (défauts dérivés de CARDINAL_URL) :
  WEBAUTHN_RP_ID     ex. cardinal.aguetai.fr   (le domaine, sans schéma ni port)
  WEBAUTHN_ORIGIN    ex. https://cardinal.aguetai.fr
En local, mettre WEBAUTHN_RP_ID=localhost et WEBAUTHN_ORIGIN=http://localhost:5000
(localhost est un « contexte sécurisé » accepté par WebAuthn).
"""

import os
from urllib.parse import urlparse

import webauthn
from webauthn.helpers import (
    options_to_json, base64url_to_bytes, bytes_to_base64url,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, ResidentKeyRequirement,
    UserVerificationRequirement, PublicKeyCredentialDescriptor,
)

RP_NAME = "Cardinal"


def _public_url() -> str:
    return os.getenv("CARDINAL_URL", "https://cardinal.aguetai.fr")


def rp_id() -> str:
    """Relying Party ID = le domaine (sans schéma ni port)."""
    if os.getenv("WEBAUTHN_RP_ID"):
        return os.environ["WEBAUTHN_RP_ID"]
    return urlparse(_public_url()).hostname or "localhost"


def origin() -> str:
    """Origine attendue (schéma + hôte + port éventuel)."""
    return os.getenv("WEBAUTHN_ORIGIN") or _public_url().rstrip("/")


# --- Enregistrement -------------------------------------------------------
def registration_options(user_id: bytes, user_name: str, exclude_ids=None) -> str:
    """Options JSON pour navigator.credentials.create(). `exclude_ids` = credentials
    déjà connus de ce compte (évite les doublons). Renvoie (json_str, challenge_bytes)."""
    exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c))
               for c in (exclude_ids or [])]
    opts = webauthn.generate_registration_options(
        rp_id=rp_id(), rp_name=RP_NAME,
        user_id=user_id, user_name=user_name, user_display_name=user_name,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=exclude,
    )
    return options_to_json(opts), opts.challenge


def verify_registration(credential, expected_challenge: bytes):
    """Vérifie l'attestation. Renvoie l'objet VerifiedRegistration
    (.credential_id, .credential_public_key, .sign_count)."""
    return webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=rp_id(),
        expected_origin=origin(),
        require_user_verification=False,
    )


# --- Authentification -----------------------------------------------------
def authentication_options() -> str:
    """Options JSON pour navigator.credentials.get() en mode « sans identifiant »
    (allowCredentials vide → passkeys découvrables). Renvoie (json_str, challenge_bytes)."""
    opts = webauthn.generate_authentication_options(
        rp_id=rp_id(),
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return options_to_json(opts), opts.challenge


def verify_authentication(credential, expected_challenge: bytes,
                          public_key: bytes, sign_count: int):
    """Vérifie l'assertion contre la clé publique stockée. Renvoie l'objet
    VerifiedAuthentication (.new_sign_count)."""
    return webauthn.verify_authentication_response(
        credential=credential,
        expected_challenge=expected_challenge,
        expected_rp_id=rp_id(),
        expected_origin=origin(),
        credential_public_key=public_key,
        credential_current_sign_count=sign_count,
        require_user_verification=False,
    )


# Ré-exports pratiques
b64url_to_bytes = base64url_to_bytes
to_b64url = bytes_to_base64url
