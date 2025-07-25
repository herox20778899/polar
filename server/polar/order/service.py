import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import stripe as stripe_lib
import structlog
from sqlalchemy import UnaryExpression, asc, desc, select
from sqlalchemy.orm import contains_eager, joinedload

from polar.account.repository import AccountRepository
from polar.auth.models import AuthSubject
from polar.billing_entry.service import billing_entry as billing_entry_service
from polar.checkout.eventstream import CheckoutEvent, publish_checkout_event
from polar.checkout.repository import CheckoutRepository
from polar.config import settings
from polar.customer_meter.service import customer_meter as customer_meter_service
from polar.customer_portal.schemas.order import CustomerOrderUpdate
from polar.customer_session.service import customer_session as customer_session_service
from polar.discount.service import discount as discount_service
from polar.email.renderer import get_email_renderer
from polar.email.sender import enqueue_email
from polar.event.service import event as event_service
from polar.event.system import SystemEvent, build_system_event
from polar.eventstream.service import publish as eventstream_publish
from polar.exceptions import PolarError, PolarRequestValidationError, ValidationError
from polar.held_balance.service import held_balance as held_balance_service
from polar.integrations.stripe.schemas import ProductType
from polar.integrations.stripe.service import stripe as stripe_service
from polar.integrations.stripe.utils import get_expandable_id
from polar.invoice.service import invoice as invoice_service
from polar.kit.address import Address
from polar.kit.db.postgres import AsyncSession
from polar.kit.metadata import MetadataQuery, apply_metadata_clause
from polar.kit.pagination import PaginationParams, paginate
from polar.kit.sorting import Sorting
from polar.kit.tax import (
    TaxabilityReason,
    TaxRate,
    from_stripe_tax_rate,
    from_stripe_tax_rate_details,
)
from polar.logging import Logger
from polar.models import (
    Checkout,
    Customer,
    Discount,
    HeldBalance,
    Order,
    OrderItem,
    Organization,
    Payment,
    Product,
    ProductPrice,
    Subscription,
    SubscriptionMeter,
    Transaction,
    User,
)
from polar.models.order import OrderBillingReason, OrderStatus
from polar.models.product import ProductBillingType
from polar.models.transaction import TransactionType
from polar.models.webhook_endpoint import WebhookEventType
from polar.notifications.notification import (
    MaintainerCreateAccountNotificationPayload,
    MaintainerNewProductSaleNotificationPayload,
    NotificationType,
)
from polar.notifications.service import PartialNotification
from polar.notifications.service import notifications as notifications_service
from polar.organization.repository import OrganizationRepository
from polar.organization.service import organization as organization_service
from polar.payment.repository import PaymentRepository
from polar.product.guard import is_custom_price
from polar.product.repository import ProductPriceRepository
from polar.subscription.repository import SubscriptionRepository
from polar.transaction.service.balance import PaymentTransactionForChargeDoesNotExist
from polar.transaction.service.balance import (
    balance_transaction as balance_transaction_service,
)
from polar.transaction.service.platform_fee import (
    platform_fee_transaction as platform_fee_transaction_service,
)
from polar.webhook.service import webhook as webhook_service
from polar.worker import enqueue_job

from .repository import OrderRepository
from .schemas import OrderInvoice, OrderUpdate
from .sorting import OrderSortProperty

log: Logger = structlog.get_logger()


class OrderError(PolarError): ...


class RecurringProduct(OrderError):
    def __init__(self, checkout: Checkout, product: Product) -> None:
        self.checkout = checkout
        self.product = product
        message = (
            f"Checkout {checkout.id} is for product {product.id}, "
            "which is a recurring product."
        )
        super().__init__(message)


class MissingCheckoutCustomer(OrderError):
    def __init__(self, checkout: Checkout) -> None:
        self.checkout = checkout
        message = f"Checkout {checkout.id} is missing a customer."
        super().__init__(message)


class MissingStripeCustomerID(OrderError):
    def __init__(self, checkout: Checkout, customer: Customer) -> None:
        self.checkout = checkout
        self.customer = customer
        message = (
            f"Checkout {checkout.id}'s customer {customer.id} "
            "is missing a Stripe customer ID."
        )
        super().__init__(message)


class NotAnOrderInvoice(OrderError):
    def __init__(self, invoice_id: str) -> None:
        self.invoice_id = invoice_id
        message = (
            f"Received invoice {invoice_id} from Stripe, but it is not an order."
            " Check if it's an issue pledge."
        )
        super().__init__(message)


class NotASubscriptionInvoice(OrderError):
    def __init__(self, invoice_id: str) -> None:
        self.invoice_id = invoice_id
        message = (
            f"Received invoice {invoice_id} from Stripe, but it it not linked to a subscription."
            " One-time purchases invoices are handled directly upon creation."
        )
        super().__init__(message)


class OrderDoesNotExist(OrderError):
    def __init__(self, invoice_id: str) -> None:
        self.invoice_id = invoice_id
        message = (
            f"Received invoice {invoice_id} from Stripe, "
            "but no associated Order exists."
        )
        super().__init__(message)


class DiscountDoesNotExist(OrderError):
    def __init__(self, invoice_id: str, coupon_id: str) -> None:
        self.invoice_id = invoice_id
        self.coupon_id = coupon_id
        message = (
            f"Received invoice {invoice_id} from Stripe with coupon {coupon_id}, "
            f"but no associated Discount exists."
        )
        super().__init__(message)


class CheckoutDoesNotExist(OrderError):
    def __init__(self, invoice_id: str, checkout_id: str) -> None:
        self.invoice_id = invoice_id
        self.checkout_id = checkout_id
        message = (
            f"Received invoice {invoice_id} from Stripe with checkout {checkout_id}, "
            f"but no associated Checkout exists."
        )
        super().__init__(message)


class SubscriptionDoesNotExist(OrderError):
    def __init__(self, invoice_id: str, stripe_subscription_id: str) -> None:
        self.invoice_id = invoice_id
        self.stripe_subscription_id = stripe_subscription_id
        message = (
            f"Received invoice {invoice_id} from Stripe "
            f"for subscription {stripe_subscription_id}, "
            f"but no associated Subscription exists."
        )
        super().__init__(message)


class AlreadyBalancedOrder(OrderError):
    def __init__(self, order: Order, payment_transaction: Transaction) -> None:
        self.order = order
        self.payment_transaction = payment_transaction
        message = (
            f"The order {order.id} with payment {payment_transaction.id} "
            "has already been balanced."
        )
        super().__init__(message)


class InvoiceAlreadyExists(OrderError):
    def __init__(self, order: Order) -> None:
        self.order = order
        message = f"An invoice already exists for order {order.id}."
        super().__init__(message, 409)


class NotPaidOrder(OrderError):
    def __init__(self, order: Order) -> None:
        self.order = order
        message = f"Order {order.id} is not paid, so an invoice cannot be generated."
        super().__init__(message, 422)


class MissingInvoiceBillingDetails(OrderError):
    def __init__(self, order: Order) -> None:
        self.order = order
        message = (
            "Billing name and address are required "
            "to generate an invoice for this order."
        )
        super().__init__(message, 422)


class InvoiceDoesNotExist(OrderError):
    def __init__(self, order: Order) -> None:
        self.order = order
        message = f"No invoice exists for order {order.id}."
        super().__init__(message, 404)


def _is_empty_customer_address(customer_address: dict[str, Any] | None) -> bool:
    return customer_address is None or customer_address["country"] is None


class OrderService:
    async def list(
        self,
        session: AsyncSession,
        auth_subject: AuthSubject[User | Organization],
        *,
        organization_id: Sequence[uuid.UUID] | None = None,
        product_id: Sequence[uuid.UUID] | None = None,
        product_billing_type: Sequence[ProductBillingType] | None = None,
        discount_id: Sequence[uuid.UUID] | None = None,
        customer_id: Sequence[uuid.UUID] | None = None,
        checkout_id: Sequence[uuid.UUID] | None = None,
        metadata: MetadataQuery | None = None,
        pagination: PaginationParams,
        sorting: list[Sorting[OrderSortProperty]] = [
            (OrderSortProperty.created_at, True)
        ],
    ) -> tuple[Sequence[Order], int]:
        repository = OrderRepository.from_session(session)
        statement = repository.get_readable_statement(auth_subject)

        statement = (
            statement.join(Order.discount, isouter=True)
            .join(Order.product)
            .options(
                *repository.get_eager_options(
                    customer_load=contains_eager(Order.customer),
                    product_load=contains_eager(Order.product),
                    discount_load=contains_eager(Order.discount),
                )
            )
        )

        if organization_id is not None:
            statement = statement.where(Customer.organization_id.in_(organization_id))

        if product_id is not None:
            statement = statement.where(Order.product_id.in_(product_id))

        if product_billing_type is not None:
            statement = statement.where(Product.billing_type.in_(product_billing_type))

        if discount_id is not None:
            statement = statement.where(Order.discount_id.in_(discount_id))

        # TODO:
        # Once we add `external_customer_id` be sure to filter for non-deleted.
        # Since it could be shared across soft deleted records whereas the unique ID cannot.
        if customer_id is not None:
            statement = statement.where(Order.customer_id.in_(customer_id))

        if checkout_id is not None:
            statement = statement.where(Order.checkout_id.in_(checkout_id))

        if metadata is not None:
            statement = apply_metadata_clause(Order, statement, metadata)

        order_by_clauses: list[UnaryExpression[Any]] = []
        for criterion, is_desc in sorting:
            clause_function = desc if is_desc else asc
            if criterion == OrderSortProperty.created_at:
                order_by_clauses.append(clause_function(Order.created_at))
            elif criterion in {OrderSortProperty.amount, OrderSortProperty.net_amount}:
                order_by_clauses.append(clause_function(Order.net_amount))
            elif criterion == OrderSortProperty.customer:
                order_by_clauses.append(clause_function(Customer.email))
            elif criterion == OrderSortProperty.product:
                order_by_clauses.append(clause_function(Product.name))
            elif criterion == OrderSortProperty.discount:
                order_by_clauses.append(clause_function(Discount.name))
            elif criterion == OrderSortProperty.subscription:
                order_by_clauses.append(clause_function(Order.subscription_id))
        statement = statement.order_by(*order_by_clauses)

        return await paginate(session, statement, pagination=pagination)

    async def get(
        self,
        session: AsyncSession,
        auth_subject: AuthSubject[User | Organization],
        id: uuid.UUID,
    ) -> Order | None:
        repository = OrderRepository.from_session(session)
        statement = (
            repository.get_readable_statement(auth_subject)
            .options(
                *repository.get_eager_options(
                    customer_load=contains_eager(Order.customer),
                    product_load=joinedload(Order.product).joinedload(
                        Product.organization
                    ),
                )
            )
            .where(Order.id == id)
        )
        return await repository.get_one_or_none(statement)

    async def update(
        self,
        session: AsyncSession,
        order: Order,
        order_update: OrderUpdate | CustomerOrderUpdate,
    ) -> Order:
        errors: list[ValidationError] = []
        invoice_locked_fields = {"billing_name", "billing_address"}
        if order.invoice_path is not None:
            for field in invoice_locked_fields:
                if field in order_update.model_fields_set and getattr(
                    order_update, field
                ) != getattr(order, field):
                    errors.append(
                        {
                            "type": "value_error",
                            "loc": ("body", field),
                            "msg": "This field cannot be updated after the invoice is generated.",
                            "input": getattr(order_update, field),
                        }
                    )

        if errors:
            raise PolarRequestValidationError(errors)

        repository = OrderRepository.from_session(session)
        order = await repository.update(
            order, update_dict=order_update.model_dump(exclude_unset=True)
        )

        await self.send_webhook(session, order, WebhookEventType.order_updated)

        return order

    async def trigger_invoice_generation(
        self, session: AsyncSession, order: Order
    ) -> None:
        if order.invoice_path is not None:
            raise InvoiceAlreadyExists(order)

        if not order.paid:
            raise NotPaidOrder(order)

        if order.billing_name is None or order.billing_address is None:
            raise MissingInvoiceBillingDetails(order)

        enqueue_job("order.invoice", order_id=order.id)

    async def generate_invoice(self, session: AsyncSession, order: Order) -> Order:
        invoice_path = await invoice_service.create_order_invoice(order)
        repository = OrderRepository.from_session(session)
        order = await repository.update(
            order, update_dict={"invoice_path": invoice_path}
        )

        await eventstream_publish(
            "order.invoice_generated",
            {"order_id": order.id},
            customer_id=order.customer_id,
            organization_id=order.product.organization_id,
        )

        await self.send_webhook(session, order, WebhookEventType.order_updated)

        return order

    async def get_order_invoice(self, order: Order) -> OrderInvoice:
        if order.invoice_path is None:
            raise InvoiceDoesNotExist(order)

        url, _ = await invoice_service.get_order_invoice_url(order)
        return OrderInvoice(url=url)

    async def create_from_checkout(
        self, session: AsyncSession, checkout: Checkout, payment: Payment | None = None
    ) -> Order:
        product = checkout.product
        if product.is_recurring:
            raise RecurringProduct(checkout, product)

        customer = checkout.customer
        if customer is None:
            raise MissingCheckoutCustomer(checkout)

        prices = product.prices

        items: list[OrderItem] = []
        for price in prices:
            if is_custom_price(price):
                item = OrderItem.from_price(price, 0, checkout.amount)
            else:
                item = OrderItem.from_price(price, 0)
            items.append(item)

        discount_amount = checkout.discount_amount

        # Retrieve tax data
        tax_amount = checkout.tax_amount or 0
        taxability_reason = None
        tax_rate: TaxRate | None = None
        tax_id = customer.tax_id
        if checkout.tax_processor_id is not None:
            calculation = await stripe_service.get_tax_calculation(
                checkout.tax_processor_id
            )
            assert tax_amount == calculation.tax_amount_exclusive
            assert len(calculation.tax_breakdown) > 0
            if len(calculation.tax_breakdown) > 1:
                log.warning(
                    "Multiple tax breakdowns found for checkout",
                    checkout_id=checkout.id,
                    calculation_id=calculation.id,
                )
            breakdown = calculation.tax_breakdown[0]
            taxability_reason = TaxabilityReason.from_stripe(
                breakdown.taxability_reason, tax_amount
            )
            tax_rate = from_stripe_tax_rate_details(breakdown.tax_rate_details)

        organization = checkout.organization
        invoice_number = await organization_service.get_next_invoice_number(
            session, organization
        )

        repository = OrderRepository.from_session(session)
        order = await repository.create(
            Order(
                status=OrderStatus.paid,
                subtotal_amount=checkout.amount,
                discount_amount=discount_amount,
                tax_amount=tax_amount,
                currency=checkout.currency,
                billing_reason=OrderBillingReason.purchase,
                billing_name=customer.billing_name,
                billing_address=customer.billing_address,
                taxability_reason=taxability_reason,
                tax_id=tax_id,
                tax_rate=tax_rate,
                invoice_number=invoice_number,
                customer=customer,
                product=product,
                discount=checkout.discount,
                subscription=None,
                checkout=checkout,
                user_metadata=checkout.user_metadata,
                custom_field_data=checkout.custom_field_data,
                items=items,
            ),
            flush=True,
        )

        # Link payment and balance transaction to the order
        if payment is not None:
            payment_repository = PaymentRepository.from_session(session)
            assert payment.amount == order.total_amount
            await payment_repository.update(payment, update_dict={"order": order})
            enqueue_job(
                "order.balance", order_id=order.id, charge_id=payment.processor_id
            )

        # Record tax transaction
        if checkout.tax_processor_id is not None:
            transaction = await stripe_service.create_tax_transaction(
                checkout.tax_processor_id, str(order.id)
            )
            await repository.update(
                order, update_dict={"tax_transaction_processor_id": transaction.id}
            )

        # Enqueue benefits grants
        enqueue_job(
            "benefit.enqueue_benefits_grants",
            task="grant",
            customer_id=customer.id,
            product_id=product.id,
            order_id=order.id,
        )

        # Trigger notifications
        await self.send_admin_notification(session, organization, order)
        await self.send_confirmation_email(session, organization, order)
        await self._on_order_created(session, order)

        return order

    async def create_order_from_stripe(
        self, session: AsyncSession, invoice: stripe_lib.Invoice
    ) -> Order:
        assert invoice.id is not None

        if invoice.metadata and invoice.metadata.get("type") in {ProductType.pledge}:
            raise NotAnOrderInvoice(invoice.id)

        if invoice.subscription is None:
            raise NotASubscriptionInvoice(invoice.id)

        # Get subscription
        stripe_subscription_id = get_expandable_id(invoice.subscription)
        subscription_repository = SubscriptionRepository.from_session(session)
        subscription = await subscription_repository.get_by_stripe_subscription_id(
            stripe_subscription_id,
            options=(
                joinedload(Subscription.product).joinedload(Product.organization),
                joinedload(Subscription.customer),
                joinedload(Subscription.meters).joinedload(SubscriptionMeter.meter),
            ),
        )
        if subscription is None:
            raise SubscriptionDoesNotExist(invoice.id, stripe_subscription_id)

        # Get customer
        customer = subscription.customer

        # Retrieve billing address
        billing_address: Address | None = None
        if customer.billing_address is not None:
            billing_address = customer.billing_address
        elif not _is_empty_customer_address(invoice.customer_address):
            billing_address = Address.model_validate(invoice.customer_address)
        # Try to retrieve the country from the payment method
        elif invoice.charge is not None:
            charge = await stripe_service.get_charge(get_expandable_id(invoice.charge))
            if payment_method_details := charge.payment_method_details:
                if card := getattr(payment_method_details, "card", None):
                    billing_address = Address.model_validate({"country": card.country})

        # Get Discount if available
        discount: Discount | None = None
        if invoice.discount is not None:
            coupon = invoice.discount.coupon
            if (metadata := coupon.metadata) is None:
                raise DiscountDoesNotExist(invoice.id, coupon.id)
            discount_id = metadata["discount_id"]
            discount = await discount_service.get(
                session, uuid.UUID(discount_id), allow_deleted=True
            )
            if discount is None:
                raise DiscountDoesNotExist(invoice.id, coupon.id)

        # Get Checkout if available
        checkout: Checkout | None = None
        invoice_metadata = invoice.metadata or {}
        subscription_metadata = (
            invoice.subscription_details.metadata or {}
            if invoice.subscription_details
            else {}
        )
        checkout_id = invoice_metadata.get("checkout_id") or subscription_metadata.get(
            "checkout_id"
        )
        subscription_details = invoice.subscription_details
        if checkout_id is not None:
            chekout_repository = CheckoutRepository.from_session(session)
            checkout = await chekout_repository.get_by_id(uuid.UUID(checkout_id))
            if checkout is None:
                raise CheckoutDoesNotExist(invoice.id, checkout_id)

        # Handle items
        product_price_repository = ProductPriceRepository.from_session(session)
        items: list[OrderItem] = []
        for line in invoice.lines:
            tax_amount = sum([tax.amount for tax in line.tax_amounts])
            product_price: ProductPrice | None = None
            price = line.price
            if price is not None:
                if price.metadata and price.metadata.get("product_price_id"):
                    product_price = await product_price_repository.get_by_id(
                        uuid.UUID(price.metadata["product_price_id"]),
                        options=product_price_repository.get_eager_options(),
                    )
                else:
                    product_price = (
                        await product_price_repository.get_by_stripe_price_id(
                            price.id,
                            options=product_price_repository.get_eager_options(),
                        )
                    )

            items.append(
                OrderItem(
                    label=line.description or "",
                    amount=line.amount,
                    tax_amount=tax_amount,
                    proration=line.proration,
                    product_price=product_price,
                )
            )

        if invoice.status == "draft":
            # Add pending billing entries
            stripe_customer_id = customer.stripe_customer_id
            assert stripe_customer_id is not None
            pending_items = await billing_entry_service.create_order_items_from_pending(
                session,
                subscription,
                stripe_invoice_id=invoice.id,
                stripe_customer_id=stripe_customer_id,
            )
            items.extend(pending_items)
            # Reload the invoice to get totals with added pending items
            if len(pending_items) > 0:
                invoice = await stripe_service.get_invoice(invoice.id)

            # Update statement descriptor
            # Stripe doesn't allow to set statement descriptor on the subscription itself,
            # so we need to set it manually on each new invoice.
            assert invoice.id is not None
            await stripe_service.update_invoice(
                invoice.id,
                statement_descriptor=subscription.organization.name[
                    : settings.stripe_descriptor_suffix_max_length
                ],
            )

        # Determine billing reason
        billing_reason = OrderBillingReason.subscription_cycle
        if invoice.billing_reason is not None:
            try:
                billing_reason = OrderBillingReason(invoice.billing_reason)
            except ValueError as e:
                log.error(
                    "Unknown billing reason, fallback to 'subscription_cycle'",
                    invoice_id=invoice.id,
                    billing_reason=invoice.billing_reason,
                )

        # Calculate discount amount
        discount_amount = 0
        if invoice.total_discount_amounts:
            for stripe_discount_amount in invoice.total_discount_amounts:
                discount_amount += stripe_discount_amount.amount

        # Retrieve tax data
        tax_amount = invoice.tax or 0
        taxability_reason: TaxabilityReason | None = None
        tax_id = customer.tax_id
        tax_rate: TaxRate | None = None
        for total_tax_amount in invoice.total_tax_amounts:
            taxability_reason = TaxabilityReason.from_stripe(
                total_tax_amount.taxability_reason, tax_amount
            )
            stripe_tax_rate = await stripe_service.get_tax_rate(
                get_expandable_id(total_tax_amount.tax_rate)
            )
            try:
                tax_rate = from_stripe_tax_rate(stripe_tax_rate)
            except ValueError:
                continue
            else:
                break

        # Ensure it inherits original metadata and custom fields
        user_metadata = (
            checkout.user_metadata
            if checkout is not None
            else subscription.user_metadata
        )
        custom_field_data = (
            checkout.custom_field_data
            if checkout is not None
            else subscription.custom_field_data
        )

        invoice_number = await organization_service.get_next_invoice_number(
            session, subscription.organization
        )

        repository = OrderRepository.from_session(session)
        order = await repository.create(
            Order(
                status=OrderStatus.paid
                if invoice.status == "paid"
                else OrderStatus.pending,
                subtotal_amount=invoice.subtotal,
                discount_amount=discount_amount,
                tax_amount=tax_amount,
                currency=invoice.currency,
                billing_reason=billing_reason,
                billing_name=customer.billing_name,
                billing_address=billing_address,
                stripe_invoice_id=invoice.id,
                taxability_reason=taxability_reason,
                tax_id=tax_id,
                tax_rate=tax_rate,
                invoice_number=invoice_number,
                customer=customer,
                product=subscription.product,
                discount=discount,
                subscription=subscription,
                checkout=checkout,
                items=items,
                user_metadata=user_metadata,
                custom_field_data=custom_field_data,
                created_at=datetime.fromtimestamp(invoice.created, tz=UTC),
            ),
            flush=True,
        )

        # Reset the associated meters, if any
        for subscription_meter in subscription.meters:
            rollover_units = await customer_meter_service.get_rollover_units(
                session, customer, subscription_meter.meter
            )
            await event_service.create_event(
                session,
                build_system_event(
                    SystemEvent.meter_reset,
                    customer=customer,
                    organization=subscription.organization,
                    metadata={"meter_id": str(subscription_meter.meter_id)},
                ),
            )
            if rollover_units > 0:
                await event_service.create_event(
                    session,
                    build_system_event(
                        SystemEvent.meter_credited,
                        customer=customer,
                        organization=subscription.organization,
                        metadata={
                            "meter_id": str(subscription_meter.meter_id),
                            "units": rollover_units,
                            "rollover": True,
                        },
                    ),
                )

        await self._on_order_created(session, order)

        return order

    async def send_admin_notification(
        self, session: AsyncSession, organization: Organization, order: Order
    ) -> None:
        product = order.product
        await notifications_service.send_to_org_members(
            session,
            org_id=product.organization_id,
            notif=PartialNotification(
                type=NotificationType.maintainer_new_product_sale,
                payload=MaintainerNewProductSaleNotificationPayload(
                    customer_name=order.customer.email,
                    product_name=product.name,
                    product_price_amount=order.net_amount,
                    organization_name=organization.slug,
                ),
            ),
        )

    async def update_order_from_stripe(
        self, session: AsyncSession, invoice: stripe_lib.Invoice
    ) -> Order:
        repository = OrderRepository.from_session(session)
        assert invoice.id is not None
        order = await repository.get_by_stripe_invoice_id(
            invoice.id, options=repository.get_eager_options()
        )
        if order is None:
            raise OrderDoesNotExist(invoice.id)

        previous_status = order.status
        status = OrderStatus.paid if invoice.status == "paid" else OrderStatus.pending
        order = await repository.update(order, update_dict={"status": status})

        # Enqueue the balance creation
        if order.paid:
            if invoice.charge:
                enqueue_job(
                    "order.balance",
                    order_id=order.id,
                    charge_id=get_expandable_id(invoice.charge),
                )
            # or if it has an associated out-of-band payment intent
            elif invoice.metadata and (
                payment_intent_id := invoice.metadata.get("payment_intent_id")
            ):
                payment_intent = await stripe_service.get_payment_intent(
                    payment_intent_id
                )
                assert payment_intent.latest_charge is not None
                enqueue_job(
                    "order.balance",
                    order_id=order.id,
                    charge_id=get_expandable_id(payment_intent.latest_charge),
                )

        await self._on_order_updated(session, order, previous_status)
        return order

    async def send_confirmation_email(
        self, session: AsyncSession, organization: Organization, order: Order
    ) -> None:
        email_renderer = get_email_renderer({"order": "polar.order"})

        product = order.product
        customer = order.customer
        token, _ = await customer_session_service.create_customer_session(
            session, customer
        )

        subject, body = email_renderer.render_from_template(
            "Your {{ product.name }} order confirmation",
            "order/confirmation.html",
            {
                "featured_organization": organization,
                "product": product,
                "url": settings.generate_frontend_url(
                    f"/{organization.slug}/portal?customer_session_token={token}&id={order.id}"
                ),
                "current_year": datetime.now().year,
            },
        )

        enqueue_email(to_email_addr=customer.email, subject=subject, html_content=body)

    async def update_product_benefits_grants(
        self, session: AsyncSession, product: Product
    ) -> None:
        statement = select(Order).where(
            Order.product_id == product.id,
            Order.deleted_at.is_(None),
            Order.subscription_id.is_(None),
        )
        orders = await session.stream_scalars(statement)
        async for order in orders:
            enqueue_job(
                "benefit.enqueue_benefits_grants",
                task="grant",
                customer_id=order.customer_id,
                product_id=product.id,
                order_id=order.id,
            )

    async def update_refunds(
        self,
        session: AsyncSession,
        order: Order,
        *,
        refunded_amount: int,
        refunded_tax_amount: int,
    ) -> Order:
        order.update_refunds(refunded_amount, refunded_tax_amount=refunded_tax_amount)
        session.add(order)
        return order

    async def create_order_balance(
        self, session: AsyncSession, order: Order, charge_id: str
    ) -> None:
        organization = order.organization
        account_repository = AccountRepository.from_session(session)
        account = await account_repository.get_by_organization(organization.id)

        # Retrieve the payment transaction and link it to the order
        payment_transaction = await balance_transaction_service.get_by(
            session, type=TransactionType.payment, charge_id=charge_id
        )
        if payment_transaction is None:
            raise PaymentTransactionForChargeDoesNotExist(charge_id)

        # Make sure to take the amount from the payment transaction and not the order
        # Orders invoices may apply customer balances which won't reflect the actual payment amount
        transfer_amount = payment_transaction.amount

        payment_transaction.order = order
        payment_transaction.payment_customer = order.customer
        session.add(payment_transaction)

        # Prepare an held balance
        # It'll be used if the account is not created yet
        held_balance = HeldBalance(
            amount=transfer_amount, order=order, payment_transaction=payment_transaction
        )

        # No account, create the held balance
        if account is None:
            held_balance.organization = organization

            # Sanity check: make sure we didn't already create a held balance for this order
            existing_held_balance = await held_balance_service.get_by(
                session,
                payment_transaction_id=payment_transaction.id,
                organization_id=organization.id,
            )
            if existing_held_balance is not None:
                raise AlreadyBalancedOrder(order, payment_transaction)

            await held_balance_service.create(session, held_balance=held_balance)

            await notifications_service.send_to_org_members(
                session=session,
                org_id=organization.id,
                notif=PartialNotification(
                    type=NotificationType.maintainer_create_account,
                    payload=MaintainerCreateAccountNotificationPayload(
                        organization_name=organization.slug,
                        url=organization.account_url,
                    ),
                ),
            )

            return

        # Sanity check: make sure we didn't already create a balance for this order
        existing_balance_transaction = await balance_transaction_service.get_by(
            session,
            type=TransactionType.balance,
            payment_transaction_id=payment_transaction.id,
            account_id=account.id,
        )
        if existing_balance_transaction is not None:
            raise AlreadyBalancedOrder(order, payment_transaction)

        # Account created, create the balance immediately
        balance_transactions = (
            await balance_transaction_service.create_balance_from_charge(
                session,
                source_account=None,
                destination_account=account,
                charge_id=charge_id,
                amount=transfer_amount,
                order=order,
            )
        )
        await platform_fee_transaction_service.create_fees_reversal_balances(
            session, balance_transactions=balance_transactions
        )

    async def send_webhook(
        self,
        session: AsyncSession,
        order: Order,
        event_type: Literal[
            WebhookEventType.order_created,
            WebhookEventType.order_updated,
            WebhookEventType.order_paid,
        ],
    ) -> None:
        await session.refresh(order.product, {"prices"})

        organization_repository = OrganizationRepository.from_session(session)
        organization = await organization_repository.get_by_id(
            order.product.organization_id
        )
        if organization is not None:
            await webhook_service.send(session, organization, event_type, order)

    async def _on_order_created(self, session: AsyncSession, order: Order) -> None:
        await self.send_webhook(session, order, WebhookEventType.order_created)
        enqueue_job("order.discord_notification", order_id=order.id)

        if order.paid:
            await self._on_order_paid(session, order)

        # Notify checkout channel that an order has been created from it
        if order.checkout:
            await publish_checkout_event(
                order.checkout.client_secret, CheckoutEvent.order_created
            )

    async def _on_order_updated(
        self, session: AsyncSession, order: Order, previous_status: OrderStatus
    ) -> None:
        await self.send_webhook(session, order, WebhookEventType.order_updated)

        became_paid = (
            order.status == OrderStatus.paid and previous_status != OrderStatus.paid
        )
        if became_paid:
            await self._on_order_paid(session, order)

    async def _on_order_paid(self, session: AsyncSession, order: Order) -> None:
        assert order.paid

        await self.send_webhook(session, order, WebhookEventType.order_paid)

        if (
            order.subscription_id is not None
            and order.billing_reason == OrderBillingReason.subscription_cycle
        ):
            enqueue_job(
                "benefit.enqueue_benefit_grant_cycles",
                subscription_id=order.subscription_id,
            )


order = OrderService()
