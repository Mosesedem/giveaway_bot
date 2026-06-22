class PaymentError(Exception):
    """Base payment error."""


class ProviderDisabledError(PaymentError):
    pass


class ProviderConfigError(PaymentError):
    pass


class VirtualAccountError(PaymentError):
    pass


class TransferError(PaymentError):
    pass


class AccountVerificationError(PaymentError):
    pass