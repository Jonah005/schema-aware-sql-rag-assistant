from django import forms
from .models import  StoreItem, DieRegister, Customer, Material, PurchaseOrder


class DieRegisterForm(forms.ModelForm):
    # Use a ModelChoiceField so users pick a material from the existing Material table
    material = forms.ModelChoiceField(queryset=Material.objects.all().order_by('material_name'), required=False)

    class Meta:
        model = DieRegister
        fields = [
            'item_code', 'item_name', 'id', 'od', 't1', 't2', 'material',
            'die_no', 'new_die', 'curing_temp', 'curing_time'
        ]

    def save(self, commit=True):
        # Override save so that if a Material instance is chosen we store its name
        instance = super().save(commit=False)
        mat = self.cleaned_data.get('material')
        if mat:
            instance.material = mat.material_name
        if commit:
            instance.save()
        return instance

class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['customer_code', 'customer_name']

class MaterialForm(forms.ModelForm):
    class Meta:
        model = Material
        fields = ['material_name']


class PurchaseOrderForm(forms.ModelForm):
    # Use HTML5 date inputs so browsers show a calendar picker
    entry_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    po_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    delivery_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    class Meta:
        model = PurchaseOrder
        # use the model's field names (lowercase) and include the date fields
        fields = ['entry_date', 'customer', 'po_no', 'po_date', 'item_description', 'quantity', 'oa_number', 'delivery_date', 'price', 'status']
        widgets = {
            'entry_date': forms.DateInput(attrs={'type': 'date'}),
            'po_date': forms.DateInput(attrs={'type': 'date'}),
            'delivery_date': forms.DateInput(attrs={'type': 'date'}),
        }