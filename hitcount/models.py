import datetime

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.db.models import F
from hitcount.utils import get_ip


# EXCEPTIONS #

class DuplicateContentObject(Exception):
    'If content_object already exists for this model'
    pass


# MANAGERS #

class HitManager(models.Manager):

    def filter_active(self, *args, **kwargs):
        '''
        Return only the 'active' hits.

        How you count a hit/view will depend on personal choice: Should the
        same user/visitor *ever* be counted twice?  After a week, or a month,
        or a year, should their view be counted again?

        The defaulf is to consider a visitor's hit still 'active' if they
        return within a the last seven days..  After that the hit
        will be counted again.  So if one person visits once a week for a year,
        they will add 52 hits to a given object.

        Change how long the expiration is by adding to settings.py:

        HITCOUNT_KEEP_HIT_ACTIVE  = {'days' : 30, 'minutes' : 30}

        Accepts days, seconds, microseconds, milliseconds, minutes,
        hours, and weeks.  It's creating a datetime.timedelta object.
        '''
        grace = getattr(settings, 'HITCOUNT_KEEP_HIT_ACTIVE', {'days':7})
        period = datetime.datetime.now() - datetime.timedelta(**grace)
        queryset = self.get_query_set()
        queryset = queryset.filter(created__gte=period)
        return queryset.filter(*args, **kwargs)

class HitCountManger(models.Manager):

    def get_for_queryset(self, queryset, cutoff_datetime=None):
        '''
        New Feature: still testing!

        Return the passed queryset with a `hits` value attached.  Optionally,
        can specify a cutoff date for the hit counts (eg, seven days ago).

        Thoughts:

        - Am returning the list unsorted, but could maybe add an option for in
          place sorting (descending or ascending).

        - This is an expensive query, should enable caching (maybe via an
          option like `cache=5 minutes` or something.
        '''
        ctype = ContentType.objects.get_for_model(queryset.model)
        qs = self.get_query_set().filter(content_type__exact=ctype.pk)

        for obj in queryset:
            try:
                hitcount = qs.get(content_type=ctype.pk, object_pk=obj.pk)
                if cutoff_datetime:
                    obj.hits = hitcount.hits_in_last(cutoff_datetime)
                else:
                    obj.hits = hitcount.hits
            except ObjectDoesNotExist:
                obj.hits = 0

        return queryset

    def update_object_count(self, content_object, request):
        '''
        Increments the hit count for the content_object with the
        request. Returns True if the request was considered a Hit; returns
        False if not.
        '''
        ctype = ContentType.objects.get_for_model(content_object)
        pk = content_object.pk
        hitcount, c = self.get_or_create(content_type = ctype, object_pk = pk)
        return self.update_hit_count(hitcount, request)

    def update_hit_count(self, hitcount, request):
        '''
        Returns True if the request was considered a Hit; returns
        False if not.
        '''
        user = request.user
        session_key = request.session.session_key
        ip = get_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:255]
        hits_per_ip_limit = getattr(settings, 'HITCOUNT_HITS_PER_IP_LIMIT', 0)
        exclude_user_group = getattr(settings,
                                     'HITCOUNT_EXCLUDE_USER_GROUP', None)

        # first, check our request against the blacklists before continuing
        if BlacklistIP.objects.filter(ip__exact = ip) or \
                BlacklistUserAgent.objects.filter(user_agent__exact = user_agent):
            return False

        # second, see if we are excluding a specific user group or not
        if exclude_user_group and user.is_authenticated():
            if user.groups.filter(name__in = exclude_user_group):
                return False

        # start with a fresh active query set (HITCOUNT_KEEP_HIT_ACTIVE )
        qs = Hit.objects.filter_active()

        # check limit on hits from a unique ip address (HITCOUNT_HITS_PER_IP_LIMIT)
        if hits_per_ip_limit:
            if qs.filter(ip = ip).count() > hits_per_ip_limit:
                return False

        # create a generic Hit object with request data
        hit = Hit(session = session_key,
                  hitcount = hitcount,
                  ip = ip,
                  user_agent = user_agent)

        # first, use a user's authentication to see if they made an earlier hit
        if user.is_authenticated():
            if not qs.filter(user = user, hitcount = hitcount):
                hit.user = user #associate this hit with a user
                hit.save()
                return True
        else:
            # if not authenticated, see if we have a repeat session
            if not qs.filter(session = session_key, hitcount = hitcount):
                hit.save()
                # forces a save on this anonymous users session
                request.session.modified = True
                return True
        return False

# MODELS #

class HitCount(models.Model):
    '''
    Model that stores the hit totals for any content object.

    '''
    hits            = models.PositiveIntegerField(default=0)
    modified        = models.DateTimeField(default=datetime.datetime.now)
    content_type    = models.ForeignKey(ContentType,
                        verbose_name="content cype",
                        related_name="content_type_set_for_%(class)s",)
    object_pk       = models.TextField('object ID')
    content_object  = generic.GenericForeignKey('content_type', 'object_pk')

    objects = HitCountManger()

    class Meta:
        ordering = ( '-hits', )
        get_latest_by = "modified"
        db_table = "hitcount_hit_count"
        verbose_name = "Hit Count"
        verbose_name_plural = "Hit Counts"

    def __unicode__(self):
        return u'%s' % self.content_object

    def save(self, *args, **kwargs):
        self.modified = datetime.datetime.now()

        if not self.pk and self.object_pk and self.content_type:
            # Because we are using a models.TextField() for `object_pk` to
            # allow *any* primary key type (integer or text), we
            # can't use `unique_together` or `unique=True` to gaurantee
            # that only one HitCount object exists for a given object.
            #
            # This is just a simple hack - if there is no `self.pk`
            # set, it checks the database once to see if the `content_type`
            # and `object_pk` exist together (uniqueness).  Obviously, this
            # is not fool proof - if someone sets their own `id` or `pk`
            # when initializing the HitCount object, we could get a duplicate.
            if HitCount.objects.filter(
                    object_pk=self.object_pk).filter(
                            content_type=self.content_type):
                raise DuplicateContentObject, "A HitCount object already " + \
                        "exists for this content_object."

        super(HitCount, self).save(*args, **kwargs)

    def hits_in_last(self, cutoff_datetime=None, **kwargs):
        '''
        Returns hit count for an object during a given time period.

        This will only work for as long as hits are saved in the Hit database.
        If you are purging your database after 45 days, for example, that means
        that asking for hits in the last 60 days will return an incorrect
        number as that the longest period it can search will be 45 days.

        For example: hits_in_last(days=7).

        Accepts days, seconds, microseconds, milliseconds, minutes,
        hours, and weeks as **kwargs - creating a datetime.timedelta object.

        Alternately, you can pass it a datetime object as a cutoff.
        '''

        if cutoff_datetime: # provide your own datetime object
            return self.hit_set.filter(created__gte=cutoff_datetime).count()

        assert kwargs, "Must provide at least one timedelta arg (eg, days=1)"
        period = datetime.datetime.now() - datetime.timedelta(**kwargs)
        return self.hit_set.filter(created__gte=period).count()

    def get_content_object_url(self):
        '''
        Django has this in its contrib.comments.model file -- seems worth
        implementing though it may take a couple steps.
        '''
        pass

class Hit(models.Model):
    '''
    Model captures a single Hit by a visitor.

    None of the fields are editable because they are all dynamically created.
    Browsing the Hit list in the Admin will allow one to blacklist both
    IP addresses and User Agents. Blacklisting simply causes those hits
    to not be counted or recorded any more.

    Depending on how long you set the HITCOUNT_KEEP_HIT_ACTIVE , and how long
    you want to be able to use `HitCount.hits_in_last(days=30)` you should
    probably also occasionally clean out this database using a cron job.

    It could get rather large.
    '''
    created         = models.DateTimeField(editable=False)
    ip              = models.CharField(max_length=40, editable=False)
    session         = models.CharField(max_length=40, editable=False)
    user_agent      = models.CharField(max_length=255, editable=False)
    user            = models.ForeignKey(User,null=True, editable=False)
    hitcount        = models.ForeignKey(HitCount, editable=False)

    class Meta:
        ordering = ( '-created', )
        get_latest_by = 'created'

    def __unicode__(self):
        return u'Hit: %s' % self.pk

    def save(self, *args, **kwargs):
        '''
        The first time the object is created and saved, we increment
        the associated HitCount object by one.  The opposite applies
        if the Hit is deleted.
        '''
        if not self.created:
            self.hitcount.hits = F('hits') + 1
            self.hitcount.save()
            self.created = datetime.datetime.now()

        super(Hit, self).save(*args, **kwargs)

    objects = HitManager()

    def delete(self, save_hitcount=False):
        '''
        If a Hit is deleted and save_hitcount=True, it will preserve the
        HitCount object's total.  However, under normal circumstances, a
        delete() will trigger a subtraction from the HitCount object's total.

        NOTE: This doesn't work during a queryset.delete() (because
        queryset.delete() skips this function).
        '''
        if not save_hitcount:
            self.hitcount.hits = F('hits') - 1
            self.hitcount.save()

        super(Hit, self).delete()

class BlacklistIP(models.Model):
    '''
    Model that holds all the blacklisted IPs.

    TODO - add a method that could import a known list of nefarious IPs.

    '''
    ip = models.CharField(max_length=40, unique=True)

    class Meta:
        db_table = "hitcount_blacklist_ip"
        verbose_name = "Blacklisted IP"
        verbose_name_plural = "Blacklisted IPs"

    def __unicode__(self):
        return u'%s' % self.ip


class BlacklistUserAgent(models.Model):
    '''
    Model that holds all the blacklisted User-Agents.

    '''
    user_agent = models.CharField(max_length=255, unique=True)

    class Meta:
        db_table = "hitcount_blacklist_user_agent"
        verbose_name = "Blacklisted User Agent"
        verbose_name_plural = "Blacklisted User Agents"

    def __unicode__(self):
        return u'%s' % self.user_agent

