#!/usr/bin/env python3
import unittest
from hospitality_quality_v2 import assess_record,sanitize_social
class QualityV2Tests(unittest.TestCase):
 def q(self,name,text='',category='hotel',website='https://examplehotel.com/'):
  return assess_record({'name':name,'category':category,'website':website},text)
 def test_ski_destination_rejected(self):
  q=self.q('Katschberg','ski area with 16 lifts, ski passes, pistes and slopes; all accommodations in the region');self.assertEqual(q['commercial_fit_tier'],'X')
 def test_hostel_rejected(self):self.assertEqual(self.q('a&o Wien Hauptbahnhof','budget hostel dormitories')['commercial_fit_tier'],'X')
 def test_hut_rejected(self):self.assertEqual(self.q('Gleiwitzer Hütte','mountain hut refuge')['commercial_fit_tier'],'X')
 def test_aggregator_rejected(self):self.assertEqual(self.q('Cappella Natura Vitalis Hotel','hotel rooms',website='http://cappella.hotels-in-tyrol.com/')['commercial_fit_tier'],'X')
 def test_generic_pm_rejected(self):self.assertEqual(self.q('Acme Property Management','property management for landlords and tenants',category='property_management')['commercial_fit_tier'],'X')
 def test_ordinary_inn_review(self):self.assertEqual(self.q('Landgasthof Lenzer','family run 3 star inn rooms restaurant')['commercial_fit_tier'],'C')
 def test_alpenhof_high_end(self):
  q=self.q('Hotel Alpenhof Hintertux','our hotel 4-star superior rooms and suites 4000 m2 spa wellness adults-only gourmet book your room € 290');self.assertEqual(q['commercial_fit_tier'],'A');self.assertTrue(q['sales_ready'])
 def test_avita_high_end(self):self.assertEqual(self.q('AVITA Resort','our resort **** Superior luxurious wellness suites infinity pool premium spa hotel guests rooms and suites')['commercial_fit_tier'],'A')
 def test_seevilla_good(self):self.assertIn(self.q('Hotel Seevilla','our hotel boutique hotel on the lake wellness spa rooms and suites gourmet book your room')['commercial_fit_tier'],{'A','B'})
 def test_vacation_rental_manager(self):self.assertIn(self.q('Alpine Luxury Villas','luxury villa rentals vacation rentals our villas private pools book your stay',category='vacation_rental',website='https://alpinevillas.com/')['commercial_fit_tier'],{'A','B'})
 def test_booking_subdomain_rejected(self):self.assertEqual(self.q('Example Hotel','our hotel rooms',website='https://foo.booking.com/hotel/gb/example.html')['commercial_fit_tier'],'X')
 def test_obertauern_destination_rejected(self):self.assertEqual(self.q('Obertauern','Austria winter sports resort with ski passes 26 cable car lift facilities slopes tourism association and hotels in Obertauern',website='https://www.obertauern.com/')['commercial_fit_tier'],'X')
 def test_real_property_near_ski_area_not_rejected(self):self.assertNotEqual(self.q('Hotel Winter Obertauern','our hotel rooms apartments spa stay with us book your room near the ski lifts',website='https://hotel-winter.at/')['commercial_fit_tier'],'X')
 def test_social_sanitization(self):
  self.assertEqual(sanitize_social('https://www.facebook.com/privacy/explanation/','facebook'),'');self.assertTrue(sanitize_social('https://www.facebook.com/pages/Berghotel-Presslauer/208323989202686/','facebook'))
if __name__=='__main__':unittest.main()
