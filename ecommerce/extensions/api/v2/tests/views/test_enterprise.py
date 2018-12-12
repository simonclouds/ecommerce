# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import datetime
import json
from uuid import uuid4

import ddt
import httpretty
import mock
from django.urls import reverse
from django.utils.timezone import now
from oscar.core.loading import get_model
from oscar.test import factories
from rest_framework import status
from waffle.models import Switch

from ecommerce.coupons.tests.mixins import CouponMixin, DiscoveryMockMixin
from ecommerce.courses.tests.factories import CourseFactory
from ecommerce.enterprise.benefits import BENEFIT_MAP as ENTERPRISE_BENEFIT_MAP
from ecommerce.enterprise.conditions import AssignableEnterpriseCustomerCondition
from ecommerce.enterprise.constants import ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH
from ecommerce.enterprise.tests.mixins import EnterpriseServiceMockMixin
from ecommerce.extensions.catalogue.tests.mixins import DiscoveryTestMixin
from ecommerce.invoice.models import Invoice
from ecommerce.programs.custom import class_path
from ecommerce.tests.mixins import ThrottlingMixin
from ecommerce.tests.testcases import TestCase

Basket = get_model('basket', 'Basket')
Benefit = get_model('offer', 'Benefit')
OfferAssignment = get_model('offer', 'OfferAssignment')
Product = get_model('catalogue', 'Product')
Voucher = get_model('voucher', 'Voucher')
VoucherApplication = get_model('voucher', 'VoucherApplication')

ENTERPRISE_COUPONS_LINK = reverse('api:v2:enterprise-coupons-list')


class TestEnterpriseCustomerView(EnterpriseServiceMockMixin, TestCase):

    dummy_enterprise_customer_data = {
        'results': [
            {
                'name': 'Starfleet Academy',
                'uuid': '5113b17bf79f4b5081cf3be0009bc96f',
                'hypothetical_private_info': 'seriously, very private',
            },
            {
                'name': 'Millennium Falcon',
                'uuid': 'd1fb990fa2784a52a44cca1118ed3993',
            }
        ]
    }

    @mock.patch('ecommerce.enterprise.utils.EdxRestApiClient')
    @httpretty.activate
    def test_get_customers(self, mock_client):
        self.mock_access_token_response()
        instance = mock_client.return_value
        setattr(
            instance,
            'enterprise-customer',
            mock.MagicMock(
                get=mock.MagicMock(
                    return_value=self.dummy_enterprise_customer_data
                )
            ),
        )
        url = reverse('api:v2:enterprise:enterprise_customers')
        result = self.client.get(url)
        self.assertEqual(result.status_code, 401)

        user = self.create_user(is_staff=True)

        self.client.login(username=user.username, password=self.password)

        result = self.client.get(url)
        self.assertEqual(result.status_code, 200)
        self.assertJSONEqual(
            result.content.decode('utf-8'),
            {
                'results': [
                    {
                        'name': 'Millennium Falcon',
                        'id': 'd1fb990fa2784a52a44cca1118ed3993'
                    },
                    {
                        'name': 'Starfleet Academy',
                        'id': '5113b17bf79f4b5081cf3be0009bc96f'
                    }  # Note that the private information from the API has been stripped
                ]
            }
        )


@ddt.ddt
class EnterpriseCouponViewSetTest(CouponMixin, DiscoveryTestMixin, DiscoveryMockMixin, ThrottlingMixin, TestCase):
    """Test the enterprise coupon order creation functionality."""
    def setUp(self):
        super(EnterpriseCouponViewSetTest, self).setUp()
        self.user = self.create_user(is_staff=True)
        self.client.login(username=self.user.username, password=self.password)

        self.data = {
            'benefit_type': Benefit.PERCENTAGE,
            'benefit_value': 100,
            'category': {'name': self.category.name},
            'code': '',
            'end_datetime': str(now() + datetime.timedelta(days=10)),
            'price': 100,
            'quantity': 2,
            'start_datetime': str(now() - datetime.timedelta(days=10)),
            'title': 'Tešt Enterprise čoupon',
            'voucher_type': Voucher.SINGLE_USE,
            'enterprise_customer': {'name': 'test enterprise', 'id': str(uuid4()).decode('utf-8')},
            'enterprise_customer_catalog': str(uuid4()).decode('utf-8'),
        }

        self.course = CourseFactory(id='course-v1:test-org+course+run', partner=self.partner)
        self.verified_seat = self.course.create_or_update_seat('verified', False, 100)

    def get_response(self, method, path, data=None):
        """Helper method for sending requests and returning the response."""
        with mock.patch(
            "ecommerce.extensions.voucher.utils.get_enterprise_customer",
            mock.Mock(return_value={'name': 'Fake enterprise'})
        ):
            if method == 'GET':
                return self.client.get(path)
            elif method == 'POST':
                return self.client.post(path, json.dumps(data), 'application/json')
            elif method == 'PUT':
                return self.client.put(path, json.dumps(data), 'application/json')
        return None

    def test_list_enterprise_coupons(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        self.create_coupon()
        self.assertEqual(Product.objects.filter(product_class__name='Coupon').count(), 2)

        response = self.client.get(ENTERPRISE_COUPONS_LINK)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        coupon_data = json.loads(response.content)['results']
        self.assertEqual(len(coupon_data), 1)
        self.assertEqual(coupon_data[0]['title'], self.data['title'])
        self.assertEqual(coupon_data[0]['client'], self.data['enterprise_customer']['name'])
        self.assertEqual(coupon_data[0]['enterprise_customer'], self.data['enterprise_customer']['id'])
        self.assertEqual(coupon_data[0]['enterprise_customer_catalog'], self.data['enterprise_customer_catalog'])
        self.assertEqual(coupon_data[0]['code_status'], 'ACTIVE')

    def test_create_ent_offers_switch_off(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': False})
        response = self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_ent_offers_switch_on(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        response = self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        coupon = Product.objects.get(title=self.data['title'])
        enterprise_customer_id = self.data['enterprise_customer']['id']
        enterprise_name = self.data['enterprise_customer']['name']
        enterprise_catalog_id = self.data['enterprise_customer_catalog']
        vouchers = coupon.attr.coupon_vouchers.vouchers.all()
        for voucher in vouchers:
            all_offers = voucher.offers.all()
            self.assertEqual(len(all_offers), 1)
            offer = all_offers[0]
            self.assertEqual(str(offer.condition.enterprise_customer_uuid), enterprise_customer_id)
            self.assertEqual(str(offer.condition.enterprise_customer_catalog_uuid), enterprise_catalog_id)
            self.assertEqual(offer.condition.proxy_class, class_path(AssignableEnterpriseCustomerCondition))
            self.assertIsNone(offer.condition.range)
            self.assertEqual(offer.benefit.proxy_class, class_path(ENTERPRISE_BENEFIT_MAP[self.data['benefit_type']]))
            self.assertEqual(offer.benefit.value, self.data['benefit_value'])
            self.assertIsNone(offer.benefit.range)

        # Check that the enterprise name took precedence as the client name
        basket = Basket.objects.filter(lines__product_id=coupon.id).first()
        invoice = Invoice.objects.get(order__basket=basket)
        self.assertEqual(invoice.business_client.name, enterprise_name)
        self.assertEqual(str(invoice.business_client.enterprise_customer_uuid), enterprise_customer_id)

    def test_update_ent_offers_switch_off(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        coupon = Product.objects.get(title=self.data['title'])

        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': False})
        response = self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'title': 'Updated Enterprise Coupon',
            }
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_ent_offers_switch_on(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        coupon = Product.objects.get(title=self.data['title'])

        self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'title': 'Updated Enterprise Coupon',
            }
        )
        updated_coupon = Product.objects.get(title='Updated Enterprise Coupon')
        self.assertEqual(coupon.id, updated_coupon.id)

    def test_update_non_ent_coupon(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        coupon = self.create_coupon()
        response = self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'title': 'Updated Enterprise Coupon',
            }
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_migrated_ent_coupon(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': False})
        self.data.update({
            'catalog_query': '*:*',
            'course_seat_types': ['verified'],
            'benefit_value': 20,
            'title': 'Migrated Enterprise Coupon',
        })
        self.get_response('POST', reverse('api:v2:coupons-list'), self.data)
        coupon = Product.objects.get(title='Migrated Enterprise Coupon')

        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        new_catalog = str(uuid4()).decode('utf-8')
        self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'enterprise_customer_catalog': new_catalog,
                'benefit_value': 50,
                'title': 'Updated Enterprise Coupon',
            }
        )
        updated_coupon = Product.objects.get(title='Updated Enterprise Coupon')
        self.assertEqual(coupon.id, updated_coupon.id)
        vouchers = updated_coupon.attr.coupon_vouchers.vouchers.all()
        for voucher in vouchers:
            all_offers = voucher.offers.all()
            self.assertEqual(len(all_offers), 2)
            original_offer = all_offers[0]
            self.assertEqual(original_offer.benefit.value, 50)
            self.assertEqual(str(original_offer.condition.range.enterprise_customer_catalog), new_catalog)
            enterprise_offer = all_offers[1]
            self.assertEqual(enterprise_offer.benefit.value, 50)
            self.assertEqual(str(enterprise_offer.condition.enterprise_customer_catalog_uuid), new_catalog)

    def test_update_max_uses_single_use(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        coupon = Product.objects.get(title=self.data['title'])
        response = self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'max_uses': 5,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_update_max_uses_invalid_value(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        self.data.update({
            'voucher_type': Voucher.MULTI_USE,
            'max_uses': 5,
        })
        self.get_response('POST', ENTERPRISE_COUPONS_LINK, self.data)
        coupon = Product.objects.get(title=self.data['title'])
        response = self.get_response(
            'PUT',
            reverse('api:v2:enterprise-coupons-detail', kwargs={'pk': coupon.id}),
            data={
                'max_uses': -5,
            }
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def assert_coupon_codes_response(self, response, coupon_id, max_uses=1, results_count=0, pagination=None):
        """
        Verify response received from `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint
        """
        coupon = Product.objects.get(id=coupon_id)

        all_coupon_codes = coupon.attr.coupon_vouchers.vouchers.values_list('code', flat=True)
        all_coupon_codes = [code for code in all_coupon_codes]
        all_received_codes = [result['code'] for result in response['results']]
        all_received_code_max_uses = [result['redemptions']['available'] for result in response['results']]

        voucher_type = coupon.attr.coupon_vouchers.vouchers.first().usage
        if results_count and voucher_type != Voucher.SINGLE_USE:
            # `max_uses` should be same for all codes
            max_uses = max_uses or 1
            self.assertEqual(set(all_received_code_max_uses), set([max_uses]))

        # total count of results returned is correct
        self.assertEqual(len(response['results']), results_count)

        # all received codes must be equals to coupon codes
        self.assertTrue(set(all_received_codes).issubset(all_coupon_codes))

        # Verify pagination.
        pagination = pagination or {'count': results_count, 'next': None, 'previous': None}
        self.assertEqual(response['count'], pagination['count'])
        self.assertEqual(response['next'], pagination['next'])
        self.assertEqual(response['previous'], pagination['previous'])

    def use_voucher(self, voucher, user):
        """
        Mark voucher as used by provided user
        """
        order = factories.OrderFactory()
        order_line = factories.OrderLineFactory(product=self.verified_seat)
        order.lines.add(order_line)
        voucher.record_usage(order, user)
        voucher.offers.first().record_usage(discount={'freq': 1, 'discount': 1})

    def create_enterprise_coupon(self, coupon_data, with_applications=False):
        """
        Create coupon and voucher applications(redemeptions) if required.
        """
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        # create coupon
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        # create some voucher applications
        if with_applications:
            max_uses = coupon_data.get('max_uses', None)
            coupon = Product.objects.get(id=coupon_id)
            for voucher in coupon.attr.coupon_vouchers.vouchers.all():
                for _ in range(max_uses or 1):
                    self.use_voucher(voucher, self.create_user())

        return coupon_id

    def assign_coupon_codes(self, coupon_id):
        """
        Assigns codes.
        """
        vouchers = Product.objects.get(id=coupon_id).attr.coupon_vouchers.vouchers.all()
        codes = [voucher.code for voucher in vouchers]
        codes_param = codes[3:]
        emails = ['t1@example.com', 't2@example.com']
        self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
            {'emails': emails, 'codes': codes_param}
        )

    @ddt.data(
        {
            'voucher_type': Voucher.SINGLE_USE,
            'quantity': 2,
            'max_uses': None,
            'expected_results_count': 2
        },
        {
            'voucher_type': Voucher.ONCE_PER_CUSTOMER,
            'quantity': 2,
            'max_uses': 2,
            'expected_results_count': 4
        },
        {
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 2,
            'max_uses': 3,
            'expected_results_count': 6
        },
    )
    def test_coupon_codes_detail(self, data):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint returns correct data for different coupons
        """
        endpoint = '/api/v2/enterprise/coupons/{}/codes/'
        pagination = {
            'count': data['expected_results_count'],
            'next': None,
            'previous': None,
        }

        coupon_id = self.create_enterprise_coupon(
            dict(self.data, voucher_type=data['voucher_type'], quantity=data['quantity'], max_uses=data['max_uses']),
            with_applications=True
        )


        # get coupon codes usage details
        response = self.get_response('GET', endpoint.format(coupon_id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = response.json()
        self.assert_coupon_codes_response(
            response,
            coupon_id,
            max_uses=data['max_uses'],
            results_count=data['expected_results_count'],
            pagination=pagination
        )

    @ddt.data(
        {
            'page': 1,
            'page_size': 2,
            'pagination': {
                'count': 6,
                'next': 'http://testserver.fake/api/v2/enterprise/coupons/{}/codes/?page=2&page_size=2',
                'previous': None,
            },
            'expected_results_count': 2,
        },
        {
            'page': 2,
            'page_size': 4,
            'pagination': {
                'count': 6,
                'next': None,
                'previous': 'http://testserver.fake/api/v2/enterprise/coupons/{}/codes/?page_size=4',
            },
            'expected_results_count': 2,
        },
        {
            'page': 2,
            'page_size': 3,
            'pagination': {
                'count': 6,
                'next': None,
                'previous': 'http://testserver.fake/api/v2/enterprise/coupons/{}/codes/?page_size=3',
            },
            'expected_results_count': 3,
        },
    )
    @ddt.unpack
    def test_coupon_codes_detail_with_pagination(self, page, page_size, pagination, expected_results_count):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint pagination works
        """
        coupon_data = {
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 2,
            'max_uses': 3,
        }
        coupon_id = self.create_enterprise_coupon(dict(self.data, **coupon_data), with_applications=True)

        # update the coupon id in `previous` and next urls
        pagination['previous'] = pagination['previous'] and pagination['previous'].format(coupon_id)
        pagination['next'] = pagination['next'] and pagination['next'].format(coupon_id)

        endpoint = '/api/v2/enterprise/coupons/{}/codes/?page={}&page_size={}'.format(coupon_id, page, page_size)

        # get coupon codes usage details
        response = self.get_response('GET', endpoint)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = response.json()
        self.assert_coupon_codes_response(
            response,
            coupon_id,
            max_uses=coupon_data['max_uses'],
            results_count=expected_results_count,
            pagination=pagination,
        )

    def test_coupon_codes_detail_with_invalid_coupon_id(self):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint returns 400 on invalid coupon id
        """
        response = self.get_response('GET', '/api/v2/enterprise/coupons/1212121212/codes/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            response.json(),
            {'detail': 'Not found.'}
        )

    def test_coupon_codes_detail_with_max_coupon_usage(self):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint returns correct default max coupon usage
        """
        coupon_data = {
            'voucher_type': Voucher.MULTI_USE,
            'quantity': 1,
            'max_uses': None
        }
        coupon_id = self.create_enterprise_coupon(
            dict(self.data, **coupon_data),
            with_applications=True
        )

        response = self.get_response('GET', '/api/v2/enterprise/coupons/{}/codes/'.format(coupon_id))
        response = response.json()
        self.assert_coupon_codes_response(
            response,
            coupon_id,
            max_uses=10000,
            results_count=1
        )

    @ddt.data(
        # default
        {
            'code_filter': '',
            'do_assignment': False,
            'with_applications': False,
            'expected_results_count': 2
        },
        {
            'code_filter': '',
            'do_assignment': True,
            'with_applications': False,
            'expected_results_count': 2
        },
        {
            'code_filter': '',
            'do_assignment': False,
            'with_applications': True,
            'expected_results_count': 2
        },
        {
            'code_filter': '',
            'do_assignment': True,
            'with_applications': True,
            'expected_results_count': 2
        },
        # not-redeemed
        {
            'code_filter': 'not-redeemed',
            'do_assignment': False,
            'with_applications': False,
            'expected_results_count': 2
        },
        {
            'code_filter': 'not-redeemed',
            'do_assignment': False,
            'with_applications': True,
            'expected_results_count': 0
        },
        {
            'code_filter': 'not-redeemed',
            'do_assignment': True,
            'with_applications': False,
            'expected_results_count': 2
        },
        {
            'code_filter': 'not-redeemed',
            'do_assignment': True,
            'with_applications': True,
            'expected_results_count': 0
        },
        # not-assigned
        {
            'code_filter': 'not-assigned',
            'do_assignment': False,
            'with_applications': False,
            'expected_results_count': 2
        },
        {
            'code_filter': 'not-assigned',
            'do_assignment': True,
            'with_applications': False,
            'expected_results_count': 0
        },
        {
            'code_filter': 'not-assigned',
            'do_assignment': False,
            'with_applications': True,
            'expected_results_count': 0
        },
        {
            'code_filter': 'not-assigned',
            'do_assignment': True,
            'with_applications': True,
            'expected_results_count': 0
        },
    )
    @ddt.unpack
    def test_coupon_codes_detail_filtering(self, code_filter, do_assignment, with_applications, expected_results_count):
        """
        Verify that `/api/v2/enterprise/coupons/{coupon_id}/codes/` endpoint returns correct data when filtered.
        """
        coupon_data = {
            'voucher_type': Voucher.MULTI_USE,
            'max_uses': 1
        }
        coupon_id = self.create_enterprise_coupon(dict(self.data, **coupon_data), with_applications=with_applications)

        if do_assignment:
            self.assign_coupon_codes(coupon_id)

        # Get coupon codes.
        response = self.get_response(
            'GET',
            '/api/v2/enterprise/coupons/{}/codes/?code_filter={}'.format(coupon_id, code_filter)
        )
        response = response.json()

        self.assert_coupon_codes_response(
            response,
            coupon_id,
            max_uses=coupon_data['max_uses'],
            results_count=expected_results_count
        )

    @ddt.data(
        (Voucher.SINGLE_USE, 2, None, ['test1@example.com', 'test2@example.com'], [1]),
        (Voucher.MULTI_USE_PER_CUSTOMER, 2, 3, ['test1@example.com', 'test2@example.com'], [3]),
        (Voucher.MULTI_USE, 1, None, ['test1@example.com', 'test2@example.com'], [2]),
        (Voucher.MULTI_USE, 2, 3, ['t1@example.com', 't2@example.com', 't3@example.com', 't4@example.com'], [3, 1]),
        (Voucher.ONCE_PER_CUSTOMER, 2, 2, ['test1@example.com', 'test2@example.com'], [2]),
    )
    @ddt.unpack
    def test_coupon_codes_assign_success(self, voucher_type, quantity, max_uses, emails, assignments_per_code):
        """Test assigning codes to users."""
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        coupon_post_data = dict(self.data, voucher_type=voucher_type, quantity=quantity, max_uses=max_uses)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        response = self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
            {'emails': emails}
        )
        response = response.json()

        for i, email in enumerate(emails):
            if voucher_type != Voucher.MULTI_USE_PER_CUSTOMER:
                assert response['offer_assignments'][i]['user_email'] == email
            else:
                for j in range(max_uses):
                    assert response['offer_assignments'][(i * max_uses) + j]['user_email'] == email

        assigned_codes = []
        for assignment in response['offer_assignments']:
            if assignment['code'] not in assigned_codes:
                assigned_codes.append(assignment['code'])

        for code in assigned_codes:
            assert OfferAssignment.objects.filter(code=code).count() in assignments_per_code

    def test_coupon_codes_assign_success_with_codes_filter(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=5)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        vouchers = Product.objects.get(id=coupon_id).attr.coupon_vouchers.vouchers.all()
        codes = [voucher.code for voucher in vouchers]
        codes_param = codes[3:]

        emails = ['t1@example.com', 't2@example.com']

        response = self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
            {'emails': emails, 'codes': codes_param}
        )
        response = response.json()

        for i, email in enumerate(emails):
            assert response['offer_assignments'][i]['user_email'] == email
            assert response['offer_assignments'][i]['code'] in codes_param

        for code in codes:
            if code not in codes_param:
                assert OfferAssignment.objects.filter(code=code).count() == 0
            else:
                assert OfferAssignment.objects.filter(code=code).count() == 1

    def test_coupon_codes_assign_success_exclude_used_codes(self):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})
        coupon_post_data = dict(self.data, voucher_type=Voucher.SINGLE_USE, quantity=5)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        vouchers = Product.objects.get(id=coupon_id).attr.coupon_vouchers.vouchers.all()
        # Use some of the vouchers
        used_codes = []
        for voucher in vouchers[:3]:
            self.use_voucher(voucher, self.create_user())
            used_codes.append(voucher.code)
        unused_codes = [voucher.code for voucher in vouchers[3:]]
        emails = ['t1@example.com', 't2@example.com']

        response = self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
            {'emails': emails}
        )
        response = response.json()

        for i, email in enumerate(emails):
            assert response['offer_assignments'][i]['user_email'] == email
            assert response['offer_assignments'][i]['code'] in unused_codes

        for code in used_codes:
            assert OfferAssignment.objects.filter(code=code).count() == 0
        for code in unused_codes:
            assert OfferAssignment.objects.filter(code=code).count() == 1

    @ddt.data(
        (Voucher.SINGLE_USE, 1, None, ['test1@example.com', 'test2@example.com']),
        (Voucher.MULTI_USE_PER_CUSTOMER, 1, 3, ['test1@example.com', 'test2@example.com']),
        (Voucher.MULTI_USE, 1, 3, ['t1@example.com', 't2@example.com', 't3@example.com', 't4@example.com']),
    )
    @ddt.unpack
    def test_coupon_codes_assign_failure(self, voucher_type, quantity, max_uses, emails):
        Switch.objects.update_or_create(name=ENTERPRISE_OFFERS_FOR_COUPONS_SWITCH, defaults={'active': True})

        coupon_post_data = dict(self.data, voucher_type=voucher_type, quantity=quantity, max_uses=max_uses)
        coupon = self.get_response('POST', ENTERPRISE_COUPONS_LINK, coupon_post_data)
        coupon = coupon.json()
        coupon_id = coupon['coupon_id']

        response = self.get_response(
            'POST',
            '/api/v2/enterprise/coupons/{}/assign/'.format(coupon_id),
            {'emails': emails}
        )
        response = response.json()
        assert response['non_field_errors'] == ['Not enough available codes for assignment!']
