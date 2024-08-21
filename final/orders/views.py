"""
Представления для REST
"""
import requests
import yaml

from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import URLValidator
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.contrib.auth import authenticate

from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from .models import (
    Category,
    Contact,
    Order,
    OrderItem,
    Parameter,
    Product,
    ProductInfo,
    ProductParameter,
    Shop,
)
from .serializers import (
    ContactSerializer,
    OrderSerializer,
    ProductInfoSerializer,
    UserSerializer,
)

from django.conf import settings


class RegisterView(APIView):
    def post(self, request):
        serializer = UserSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()

            # Попытка отправки email
            try:
                send_mail(
                    'Welcome to our service',
                    'Thank you for registering.',
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    fail_silently=False,
                )
                email_status = "Email sent successfully"
            except Exception as e:
                email_status = f"Registration successful, but failed to send email: {str(e)}"

            return Response({"status": "User created successfully", "email_status": email_status},
                            status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class LoginView(APIView):
    def post(self, request):
        email = request.data.get("email")
        password = request.data.get("password")
        user = authenticate(request, username=email, password=password)
        if user is not None:
            token, created = Token.objects.get_or_create(user=user)
            return Response(
                {"status": "Login successful", "token": token.key},
                status=status.HTTP_200_OK,
            )
        else:
            return Response(
                {"status": "Invalid credentials"}, status=status.HTTP_401_UNAUTHORIZED
            )


class ProductInfoViewSet(ModelViewSet):
    queryset = ProductInfo.objects.all()
    serializer_class = ProductInfoSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        category = self.request.query_params.get("category")
        if category:
            queryset = queryset.filter(product__category__name=category)
        return queryset


class CartView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        order = get_object_or_404(Order, user=request.user, status="basket")
        serializer = OrderSerializer(order)
        return Response(serializer.data)

    def post(self, request):
        order, created = Order.objects.get_or_create(user=request.user, status="basket")
        product_info_id = request.data.get("product_info_id")
        quantity = request.data.get("quantity")
        product_info = get_object_or_404(ProductInfo, id=product_info_id)
        order_item, created = OrderItem.objects.get_or_create(
            order=order, product_info=product_info, defaults={"quantity": quantity}
        )
        if not created:
            order_item.quantity += int(quantity)
            order_item.save()
        serializer = OrderSerializer(order)
        return Response(serializer.data)

    def delete(self, request):
        order = get_object_or_404(Order, user=request.user, status="basket")
        product_info_id = request.data.get("product_info_id")
        product_info = get_object_or_404(ProductInfo, id=product_info_id)
        order_item = get_object_or_404(
            OrderItem, order=order, product_info=product_info
        )
        order_item.delete()
        serializer = OrderSerializer(order)
        return Response(serializer.data)


class ContactViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ContactSerializer

    def get_queryset(self):
        return Contact.objects.filter(user=self.request.user)


class ConfirmOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        contact_id = request.data.get("contact_id")

        try:
            contact = Contact.objects.get(id=contact_id, user=user)
            order = Order.objects.get(user=user, status="basket")
            order.contact = contact
            order.status = "confirmed"
            order.save()

            # Попытка отправки email
            try:
                send_mail(
                    'Order Confirmation',
                    f'Your order #{order.id} has been confirmed.',
                    settings.DEFAULT_FROM_EMAIL,
                    [user.email],
                    fail_silently=False,
                )
                email_status = "Email sent successfully"
            except Exception as e:
                email_status = f"Order confirmed, but failed to send email: {str(e)}"

            return Response({"status": "Order confirmed", "email_status": email_status}, status=status.HTTP_200_OK)

        except Contact.DoesNotExist:
            return Response({"error": "Contact not found"}, status=status.HTTP_400_BAD_REQUEST)

        except Order.DoesNotExist:
            return Response({"error": "Basket not found"}, status=status.HTTP_400_BAD_REQUEST)


class OrderViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = OrderSerializer

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).exclude(status="basket")


class ImportProducts(APIView):
    """
    Класс для импорта товаров от поставщика
    """

    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        user = request.user

        if not user.is_superuser and user.type != "shop":
            return JsonResponse(
                {
                    "status": False,
                    "error": "Access denied. Only shops can import products.",
                },
                status=403,
            )

        url = request.data.get("url")
        file = request.FILES.get("file")

        if not url and not file:
            return JsonResponse(
                {"status": False, "error": "URL or file is required."}, status=400
            )

        try:
            if url:
                validate_url = URLValidator()
                validate_url(url)
                response = requests.get(url)
                response.raise_for_status()
                data = yaml.safe_load(response.content)
            elif file:
                data = yaml.safe_load(file.read())
        except ValidationError as e:
            return JsonResponse({"status": False, "error": "Invalid URL."}, status=400)
        except requests.RequestException as e:
            return JsonResponse(
                {"status": False, "error": "Failed to fetch the file from URL."},
                status=400,
            )
        except yaml.YAMLError as e:
            return JsonResponse(
                {"status": False, "error": "Invalid YAML file."}, status=400
            )

        with transaction.atomic():
            try:
                shop, created = Shop.objects.get_or_create(name=data["shop"], user=user)
                if created:
                    shop.url = url or ""
                    shop.save()

                categories = data.get("categories", [])
                for category_data in categories:
                    category, _ = Category.objects.get_or_create(
                        id=category_data["id"], defaults={"name": category_data["name"]}
                    )
                    category.shops.add(shop)
                    category.save()

                goods = data.get("goods", [])
                for item in goods:
                    product, _ = Product.objects.get_or_create(
                        name=item["name"], defaults={"category_id": item["category"]}
                    )

                    product_info = ProductInfo.objects.create(
                        product=product,
                        shop=shop,
                        name=item["model"],
                        quantity=item["quantity"],
                        price=item["price"],
                        price_rrc=item["price_rrc"],
                    )

                    parameters = item.get("parameters", {})
                    for param_name, param_value in parameters.items():
                        parameter, _ = Parameter.objects.get_or_create(name=param_name)
                        ProductParameter.objects.create(
                            product_info=product_info,
                            parameter=parameter,
                            value=param_value,
                        )

                return JsonResponse(
                    {"status": True, "message": "Products imported successfully."}
                )

            except Exception as e:
                return JsonResponse({"status": False, "error": str(e)}, status=500)
